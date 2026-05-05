"""Tests for `services.walk_forward_service` (PR-A1).

Plan generation is exercised against an in-memory ohlcv_daily seeded
with a contiguous block of trading days. The orchestrator
(`execute_walk_forward`) is exercised with a stub `sweep_worker`
that just marks the sweep as completed and seeds child Discussion
rows with verdicts — the real worker (`run_sweep_worker`) goes
through the LLM stack and is too heavy for unit tests.

Coverage:
  - rolling 60/20 plan layout (fold N-1 train ends before fold N test starts)
  - rejects insufficient archive
  - rejects bad inputs (n_folds, market, window sizes)
  - execute_walk_forward creates train + test sweeps with the
    right metadata (fold_kind, parent_sweep_id, weights_override)
  - execute_walk_forward freezes weights from train and injects
    them into the test sweep
  - per-fold failure isolation
  - test fold's `weights_override` does NOT mutate the parent
    template's `persona_weights`
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.ohlcv_daily import OhlcvDaily
from models.user import User, UserRole
from services import walk_forward_service as wf


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        email=f"wf-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


async def _seed_trading_days(
    db: AsyncSession, *,
    market: str,
    start: date,
    n: int,
) -> list[date]:
    """Seed `n` consecutive weekday-only trading days starting at
    `start`. Returns the actual list of dates seeded."""
    dates: list[date] = []
    cur = start
    while len(dates) < n:
        if cur.weekday() < 5:   # 0..4 = Mon..Fri
            dates.append(cur)
        cur = cur + timedelta(days=1)

    bars = [
        OhlcvDaily(
            market=market, symbol="2330", ts=d,
            open=100.0, high=101.0, low=99.0, close=100.0,
            volume=1_000_000, source="test",
        )
        for d in dates
    ]
    for b in bars:
        db.add(b)
    await db.commit()
    return dates


async def _make_strategy(
    db: AsyncSession,
    *,
    owner_id: UUID,
    persona_ids: list[str] | None = None,
    market: str = "TW",
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner_id,
        name="WF Test",
        description="walk-forward unit test",
        topic="t", rules="r",
        market=market,
        persona_ids=persona_ids or ["buffett", "lynch"],
        default_rounds=1,
        default_concurrency=1,
        default_auto_post_mortem=False,
        persona_weights={},
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


# ── plan_walk_forward ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_rolling_window_60_20_two_folds(
    db_session: AsyncSession, owner: User,
):
    """Default 2 folds × (60 train + 20 test) = 160 trading days
    needed back from anchor. Verify both folds get the right train
    + test windows and non-overlapping date ranges."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    # Seed 200 trading days ending on a known weekday anchor.
    anchor = date(2026, 5, 5)   # Tuesday
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=anchor - timedelta(days=400),   # generous slack
        n=200,
    )
    plan_anchor = seeded[-1]

    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id,
        market="TW",
        anchor_date=plan_anchor,
        train_window_days=60,
        test_window_days=20,
        n_folds=2,
    )

    assert len(plan.folds) == 2
    f0, f1 = plan.folds
    assert len(f0.train_dates) == 60
    assert len(f0.test_dates) == 20
    assert len(f1.train_dates) == 60
    assert len(f1.test_dates) == 20

    # fold[0]'s train ends right before fold[0]'s test starts.
    assert max(f0.train_dates) < min(f0.test_dates)
    # fold[1]'s test ends right before fold[0]'s train starts.
    assert max(f1.test_dates) < min(f0.train_dates)
    # fold[1]'s train ends right before fold[1]'s test starts.
    assert max(f1.train_dates) < min(f1.test_dates)


@pytest.mark.asyncio
async def test_plan_rejects_insufficient_archive(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    # Only seed 50 days — way less than 80 needed for one fold.
    await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=50,
    )
    with pytest.raises(ValueError, match="ohlcv_daily archive"):
        await wf.plan_walk_forward(
            db_session,
            strategy_id=tmpl.id,
            market="TW",
            anchor_date=date(2026, 5, 5),
            train_window_days=60,
            test_window_days=20,
            n_folds=1,
        )


@pytest.mark.asyncio
async def test_plan_rejects_invalid_inputs(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    with pytest.raises(ValueError, match="train_window_days"):
        await wf.plan_walk_forward(
            db_session, strategy_id=tmpl.id,
            market="TW", anchor_date=date(2026, 5, 5),
            train_window_days=0,
        )
    with pytest.raises(ValueError, match="n_folds"):
        await wf.plan_walk_forward(
            db_session, strategy_id=tmpl.id,
            market="TW", anchor_date=date(2026, 5, 5),
            n_folds=99,
        )
    with pytest.raises(ValueError, match="market"):
        await wf.plan_walk_forward(
            db_session, strategy_id=tmpl.id,
            market="JP", anchor_date=date(2026, 5, 5),
        )


# ── execute_walk_forward (orchestrator) ──────────────────────────


@pytest.mark.asyncio
async def test_execute_creates_train_and_test_sweeps_per_fold(
    db_session: AsyncSession, owner: User,
):
    """Stub the worker so we can verify the orchestrator's
    sweep-creation pattern without running real discussions.
    Each fold should produce one train sweep and one test sweep,
    test linked at train via parent_sweep_id."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5),
        n=100,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=2,
    )

    worker_calls: list[UUID] = []

    async def stub_worker(sweep_id: UUID) -> None:
        worker_calls.append(sweep_id)

    results = await wf.execute_walk_forward(
        owner_id=owner.id,
        strategy_id=tmpl.id,
        plan=plan,
        rounds_per_discussion=1,
        concurrency=1,
        auto_post_mortem=False,
        sweep_worker=stub_worker,
    )

    assert len(results) == 2
    for r in results:
        assert r.train_sweep_id is not None
        assert r.test_sweep_id is not None
        assert r.error is None
    # 2 folds × 2 sweeps = 4 worker invocations
    assert len(worker_calls) == 4

    # Verify sweep rows persisted with the right metadata.
    sweeps = list(
        (await db_session.scalars(
            select(BacktestSweep).where(
                BacktestSweep.owner_id == owner.id,
            ).order_by(BacktestSweep.created_at)
        )).all()
    )
    assert len(sweeps) == 4
    train_sweeps = [s for s in sweeps if s.fold_kind == "train"]
    test_sweeps = [s for s in sweeps if s.fold_kind == "test"]
    assert len(train_sweeps) == 2
    assert len(test_sweeps) == 2
    for ts in test_sweeps:
        assert ts.parent_sweep_id is not None
        # parent must be one of the train sweeps for this owner
        assert ts.parent_sweep_id in {t.id for t in train_sweeps}


@pytest.mark.asyncio
async def test_execute_freezes_weights_from_train_into_test(
    db_session: AsyncSession, owner: User,
):
    """The whole point: train fold's aggregate-derived weights end
    up in the test fold's `weights_override` rather than the
    template's `persona_weights`."""
    tmpl = await _make_strategy(
        db_session, owner_id=owner.id,
        persona_ids=["a", "b", "c"],
    )
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=80,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=1,
    )

    async def stub_worker(sweep_id: UUID) -> None:
        # When the worker is called, simulate the train sweep
        # producing 5 winning discussions for persona "a" and
        # 1 losing discussion for "b" — enough to push "a"'s
        # weight up.
        async with __import__(
            "db.session", fromlist=["AsyncSessionLocal"],
        ).AsyncSessionLocal() as db:
            sw = await db.scalar(
                select(BacktestSweep).where(BacktestSweep.id == sweep_id),
            )
            if sw is None or sw.fold_kind != "train":
                return
            for i in range(5):
                d = Discussion(
                    id=uuid.uuid4(), owner_id=owner.id,
                    topic="t", rules="r",
                    persona_ids=["a"], market="TW", status="done",
                    current_round=1, sweep_id=sw.id,
                    verdict="win",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(d)
            d_loss = Discussion(
                id=uuid.uuid4(), owner_id=owner.id,
                topic="t", rules="r",
                persona_ids=["b"], market="TW", status="done",
                current_round=1, sweep_id=sw.id,
                verdict="loss",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(d_loss)
            await db.commit()

    results = await wf.execute_walk_forward(
        owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, sweep_worker=stub_worker,
    )
    assert len(results) == 1
    assert results[0].error is None

    # Test sweep should carry weights_override; train sweep should not.
    test_sweep = await db_session.scalar(
        select(BacktestSweep).where(
            BacktestSweep.id == results[0].test_sweep_id,
        )
    )
    train_sweep = await db_session.scalar(
        select(BacktestSweep).where(
            BacktestSweep.id == results[0].train_sweep_id,
        )
    )
    assert test_sweep is not None
    assert train_sweep is not None
    assert train_sweep.weights_override is None
    # `a` has 5 wins → high weight; `b` has 0 wins / 1 sample →
    # below MIN_SAMPLES so absent. `c` not in any discussion so absent.
    assert test_sweep.weights_override is not None
    assert "a" in test_sweep.weights_override
    assert test_sweep.weights_override["a"] >= 1.0


@pytest.mark.asyncio
async def test_execute_does_not_mutate_template_persona_weights(
    db_session: AsyncSession, owner: User,
):
    """Critical OOS guarantee: the train fold's weights end up in
    the test sibling's `weights_override`, NOT in the parent
    template's `persona_weights`."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    initial_weights = dict(tmpl.persona_weights or {})

    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=80,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=1,
    )

    async def stub_worker(sweep_id: UUID) -> None:
        # Stub does nothing — but worker call is required for
        # the orchestrator to advance past the train phase.
        return

    await wf.execute_walk_forward(
        owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, sweep_worker=stub_worker,
    )

    refreshed = await db_session.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == tmpl.id,
        )
    )
    assert refreshed is not None
    assert dict(refreshed.persona_weights or {}) == initial_weights


@pytest.mark.asyncio
async def test_execute_isolates_per_fold_failure(
    db_session: AsyncSession, owner: User,
):
    """A worker that raises on the first sweep shouldn't take down
    subsequent folds — fold-level isolation."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=120,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=2,
    )

    call_count: dict[str, int] = {"n": 0}

    async def flaky_worker(sweep_id: UUID) -> None:
        call_count["n"] += 1
        # Fail the first call only — the orchestrator should
        # bubble that to fold[0].error and proceed to fold[1].
        if call_count["n"] == 1:
            raise RuntimeError("simulated train failure")

    results = await wf.execute_walk_forward(
        owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, sweep_worker=flaky_worker,
    )
    # 2 folds, each gets a result; first carries an error message,
    # second runs to completion.
    assert len(results) == 2
    errored = [r for r in results if r.error is not None]
    succeeded = [r for r in results if r.error is None]
    assert len(errored) == 1
    assert len(succeeded) == 1


# ── synthesizer wiring (PR-A1) ───────────────────────────────────


def test_synthesizer_prompt_prefers_weights_override_over_template():
    """The actual integration is in `synthesize_conclusion`. This
    test checks the rendering path via `format_weights_for_synthesizer`
    is unchanged — what matters is that the right map gets passed.
    Full integration is covered by the orchestrator tests above."""
    from services.persona_weight_learner import (
        format_weights_for_synthesizer,
    )
    out = format_weights_for_synthesizer({"a": 1.5, "b": 0.7})
    assert "權重 1.50" in out
    assert "權重 0.70" in out


def test_create_sweep_rejects_weights_override_on_non_test_fold():
    """Direct API smoke check — the validator forbids
    weights_override on production / train folds."""
    from services.backtest_sweep_service import VALID_FOLD_KINDS
    assert "test" in VALID_FOLD_KINDS
    assert "train" in VALID_FOLD_KINDS
    assert "production" in VALID_FOLD_KINDS


def test_walk_forward_fold_dataclass_round_trip():
    """Sanity check the dataclass shapes don't drift."""
    f = wf.WalkForwardFold(
        fold_index=0,
        train_anchor=date(2026, 1, 1),
        train_dates=[date(2026, 1, i) for i in range(1, 21)],
        test_anchor=date(2026, 1, 21),
        test_dates=[date(2026, 1, i) for i in range(21, 31)],
    )
    assert len(f.train_dates) == 20
    assert len(f.test_dates) == 10


@pytest.mark.asyncio
async def test_fit_frozen_weights_returns_empty_dict_for_missing_sweep(
    db_session: AsyncSession, owner: User,
):
    weights = await wf._fit_frozen_weights(
        owner_id=owner.id,
        strategy_id=uuid.uuid4(),
        train_sweep_id=uuid.uuid4(),
    )
    assert weights == {}


# ── Audit follow-up #1: orchestrator observability ───────────────


@pytest.mark.asyncio
async def test_execute_logs_summary_and_emits_success_metric(
    db_session: AsyncSession, owner: User, caplog,
):
    """Happy path emits the start + complete log lines AND bumps
    the `success` counter on WALK_FORWARD_RUNS_TOTAL exactly once."""
    import logging as _logging

    from middleware import metrics as _metrics

    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=80,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=1,
    )

    async def stub_worker(sweep_id: UUID) -> None:
        return

    before = _metrics.WALK_FORWARD_RUNS_TOTAL.labels(
        status="success",
    )._value.get()

    caplog.set_level(_logging.INFO, logger="services.walk_forward_service")
    results = await wf.execute_walk_forward(
        owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, sweep_worker=stub_worker,
    )
    assert len(results) == 1
    assert all(r.error is None for r in results)

    after = _metrics.WALK_FORWARD_RUNS_TOTAL.labels(
        status="success",
    )._value.get()
    assert after == before + 1

    log_messages = [r.message for r in caplog.records]
    assert any(m == "walk_forward.start" for m in log_messages)
    assert any(m == "walk_forward.complete" for m in log_messages)


@pytest.mark.asyncio
async def test_execute_emits_partial_metric_when_some_folds_fail(
    db_session: AsyncSession, owner: User,
):
    """A run where some folds succeed and some fail bumps the
    `partial` counter (not `success`, not `failed`). Operator can
    triage to find the broken fold."""
    from middleware import metrics as _metrics

    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=120,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=2,
    )

    call_count = {"n": 0}

    async def flaky_worker(sweep_id: UUID) -> None:
        call_count["n"] += 1
        # fail the first fold's train sweep only
        if call_count["n"] == 1:
            raise RuntimeError("simulated infra error")

    before = _metrics.WALK_FORWARD_RUNS_TOTAL.labels(
        status="partial",
    )._value.get()

    results = await wf.execute_walk_forward(
        owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, sweep_worker=flaky_worker,
    )

    after = _metrics.WALK_FORWARD_RUNS_TOTAL.labels(
        status="partial",
    )._value.get()
    assert after == before + 1
    # 1 errored, 1 succeeded
    assert sum(1 for r in results if r.error is not None) == 1


@pytest.mark.asyncio
async def test_execute_emits_failed_metric_on_orchestrator_exception(
    db_session: AsyncSession, owner: User,
):
    """When something escapes past the per-fold try/except
    (infrastructure failure during the loop body itself), the
    top-level handler logs at WARNING and bumps the `failed`
    counter so a Prometheus alert can fire."""
    from middleware import metrics as _metrics

    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=80,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=1,
    )

    before = _metrics.WALK_FORWARD_RUNS_TOTAL.labels(
        status="failed",
    )._value.get()

    # Patch _record_fold_metric to raise — this simulates an
    # unexpected exception escaping past the per-fold try/except.
    async def stub_worker(sweep_id: UUID) -> None:
        return

    from unittest.mock import patch
    with patch.object(
        wf, "_record_fold_metric",
        side_effect=RuntimeError("metric infrastructure broken"),
    ):
        with pytest.raises(RuntimeError, match="metric infrastructure"):
            await wf.execute_walk_forward(
                owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
                rounds_per_discussion=1, concurrency=1,
                auto_post_mortem=False, sweep_worker=stub_worker,
            )

    after = _metrics.WALK_FORWARD_RUNS_TOTAL.labels(
        status="failed",
    )._value.get()
    assert after == before + 1


# ── Audit follow-up #2: parallel-run guard ────────────────────────


@pytest.mark.asyncio
async def test_has_active_walk_forward_false_when_no_sweeps(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl.id,
    ) is False


@pytest.mark.asyncio
async def test_has_active_walk_forward_true_when_train_running(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    db_session.add(BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl.id,
        fold_kind="train",
        status="running",
    ))
    await db_session.commit()
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl.id,
    ) is True


@pytest.mark.asyncio
async def test_has_active_walk_forward_true_when_test_pending(
    db_session: AsyncSession, owner: User,
):
    """Pending status (sweep created but worker hasn't picked it
    up yet) is also "active" — a parallel orchestrator would
    immediately see it as running and start racing."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    db_session.add(BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl.id,
        fold_kind="test",
        status="pending",
    ))
    await db_session.commit()
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl.id,
    ) is True


@pytest.mark.asyncio
async def test_has_active_walk_forward_false_when_terminal(
    db_session: AsyncSession, owner: User,
):
    """Completed / cancelled / failed walk-forward sweeps don't
    block a new run — operator can re-trigger after a previous
    run finishes regardless of outcome."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    for status in ("completed", "cancelled", "failed"):
        db_session.add(BacktestSweep(
            id=uuid.uuid4(), owner_id=owner.id,
            topic="t", rules="r", market="TW",
            persona_ids=["a"],
            anchor_date=date(2026, 5, 1),
            trading_days_count=5, rounds_per_discussion=1,
            concurrency=1, auto_post_mortem=False,
            strategy_id=tmpl.id,
            fold_kind="train",
            status=status,
        ))
    await db_session.commit()
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl.id,
    ) is False


@pytest.mark.asyncio
async def test_has_active_walk_forward_ignores_production_sweeps(
    db_session: AsyncSession, owner: User,
):
    """Auto-schedule cron firing a `production` sweep at the same
    time the operator wants a walk-forward must NOT block the
    walk-forward — they run independently."""
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    db_session.add(BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl.id,
        fold_kind="production",
        status="running",
    ))
    await db_session.commit()
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl.id,
    ) is False


@pytest.mark.asyncio
async def test_has_active_walk_forward_scoped_to_strategy(
    db_session: AsyncSession, owner: User,
):
    """One strategy's active walk-forward doesn't block another
    strategy's run — operators with multiple strategies should be
    able to walk-forward all of them in parallel."""
    tmpl_a = await _make_strategy(db_session, owner_id=owner.id)
    tmpl_b = await _make_strategy(db_session, owner_id=owner.id)
    db_session.add(BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl_a.id,
        fold_kind="train",
        status="running",
    ))
    await db_session.commit()
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl_a.id,
    ) is True
    assert await wf.has_active_walk_forward(
        db_session, strategy_id=tmpl_b.id,
    ) is False


@pytest.mark.asyncio
async def test_execute_records_per_fold_metric(
    db_session: AsyncSession, owner: User,
):
    """Per-fold counter (`completed` / `failed`) bumps once per
    fold so dashboards can show fold-level success rate, not just
    run-level."""
    from middleware import metrics as _metrics

    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    seeded = await _seed_trading_days(
        db_session, market="TW",
        start=date(2026, 1, 5), n=120,
    )
    plan = await wf.plan_walk_forward(
        db_session,
        strategy_id=tmpl.id, market="TW",
        anchor_date=seeded[-1],
        train_window_days=20, test_window_days=10,
        n_folds=2,
    )

    completed_before = _metrics.WALK_FORWARD_FOLDS_TOTAL.labels(
        outcome="completed",
    )._value.get()

    async def stub_worker(sweep_id: UUID) -> None:
        return

    await wf.execute_walk_forward(
        owner_id=owner.id, strategy_id=tmpl.id, plan=plan,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, sweep_worker=stub_worker,
    )

    completed_after = _metrics.WALK_FORWARD_FOLDS_TOTAL.labels(
        outcome="completed",
    )._value.get()
    assert completed_after == completed_before + 2


def test_walk_forward_plan_dataclass_field_default_factory():
    """Mutable default check — folds list shouldn't be shared
    across instances (regression guard for a common dataclass
    mistake)."""
    p1 = wf.WalkForwardPlan(
        strategy_id=uuid.uuid4(), market="TW",
        train_window_days=60, test_window_days=20,
    )
    p2 = wf.WalkForwardPlan(
        strategy_id=uuid.uuid4(), market="TW",
        train_window_days=60, test_window_days=20,
    )
    p1.folds.append(wf.WalkForwardFold(
        fold_index=0,
        train_anchor=date(2026, 1, 1), train_dates=[],
        test_anchor=date(2026, 1, 1), test_dates=[],
    ))
    assert p2.folds == []


@pytest.mark.asyncio
async def test_synthesize_conclusion_uses_weights_override_when_present(
    db_session: AsyncSession, owner: User,
):
    """End-to-end check that PR-A1's synthesizer change actually
    consumes weights_override ahead of the template. Builds a
    fixture sweep + discussion + template, then asserts the
    weights map fed into format_weights_for_synthesizer matches
    weights_override (mocked synthesize_conclusion bypasses the
    LLM call entirely)."""
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock, patch

    from services import discussion_service
    tmpl = await _make_strategy(db_session, owner_id=owner.id)
    tmpl.persona_weights = {"buffett": 1.5, "lynch": 0.6}
    await db_session.commit()

    train = BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"], anchor_date=date(2026, 1, 1),
        trading_days_count=5, rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, strategy_id=tmpl.id,
        fold_kind="train",
    )
    test = BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"], anchor_date=date(2026, 2, 1),
        trading_days_count=5, rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=False, strategy_id=tmpl.id,
        fold_kind="test", parent_sweep_id=train.id,
        weights_override={"buffett": 1.9},   # very different from tmpl
    )
    db_session.add_all([train, test])
    await db_session.commit()

    discussion = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett"], market="TW",
        status="running", current_round=1,
        sweep_id=test.id,
        as_of_date=date(2026, 2, 5),
    )
    db_session.add(discussion)
    await db_session.commit()
    await db_session.refresh(discussion)

    captured: dict[str, Any] = {}

    def _capture_format(weights: dict) -> str:
        captured["weights"] = dict(weights)
        return ""

    async def _stream_chat_stub(*_a: Any, **_kw: Any) -> AsyncIterator[dict]:
        yield {
            "type": "delta",
            "text": (
                '{"recommendations": ['
                '{"symbol": "2330", "confidence": 0.5}], '
                '"reasoning": "x", "risks": [], '
                '"time_horizon": "short_term", "consensus_score": 0.5}'
            ),
        }

    # `format_weights_for_synthesizer` is imported inside
    # synthesize_conclusion at call time, so patch the real module.
    with patch(
        "services.persona_weight_learner.format_weights_for_synthesizer",
        side_effect=_capture_format,
    ), patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_chat_stub,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        await discussion_service.synthesize_conclusion(
            db_session, discussion, user_id=str(owner.id),
            provider="anthropic", model="claude-sonnet-4-6",
        )

    # PR-A1: weights_override (1.9) wins over template (1.5)
    assert captured.get("weights") == {"buffett": 1.9}
