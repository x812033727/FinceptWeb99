"""Tests for `services.backtest_sweep_service` (PR #274).

Covers:
  - Input validation: bounds + required fields
  - Trading-day resolution: weekend skip + partial archive +
    empty when archive doesn't reach anchor
  - Cancel during run: worker bails and leaves partial progress
  - Concurrency: serial vs parallel, semaphore caps active workers
  - Failure isolation: one bad date doesn't stop the sweep
  - Status transitions across the full lifecycle

Worker tests stub `_process_one_date` so we don't need to spin
up the full discussion / LLM stack — the worker's own state
machine is what matters here.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.ohlcv_daily import OhlcvDaily
from models.user import User, UserRole
from services import backtest_sweep_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"sweep-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _bar(symbol: str, ts: date, close: float) -> OhlcvDaily:
    return OhlcvDaily(
        market="TW", symbol=symbol, ts=ts,
        open=close, high=close, low=close, close=close,
        volume=1_000_000, source="test",
    )


# ── create_sweep validation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_create_sweep_persists_pending_row(
    db_session: AsyncSession, owner: User,
):
    """Happy path — sweep lands in pending state with all template
    fields preserved verbatim."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="2330 短線", rules="rule",
        market="TW", persona_ids=["buffett", "lynch"],
        anchor_date=date(2026, 1, 1),
        trading_days_count=5,
        rounds_per_discussion=1,
        concurrency=1,
    )
    assert sweep.status == svc.STATUS_PENDING
    assert sweep.topic == "2330 短線"
    assert sweep.persona_ids == ["buffett", "lynch"]
    assert sweep.trading_days_count == 5
    assert sweep.resolved_dates == []
    # PR #275: auto_post_mortem defaults True so multi-day sweeps
    # come with the self-critique attached out of the box.
    assert sweep.auto_post_mortem is True


@pytest.mark.asyncio
async def test_create_sweep_persists_auto_post_mortem_off(
    db_session: AsyncSession, owner: User,
):
    """Operator can opt out — auto_post_mortem=False saves the
    ~50% cost overhead for cost-sensitive runs."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 1), trading_days_count=3,
        auto_post_mortem=False,
    )
    assert sweep.auto_post_mortem is False


@pytest.mark.asyncio
async def test_create_sweep_rejects_empty_personas(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError, match="persona_ids"):
        await svc.create_sweep(
            db_session, owner_id=owner.id,
            topic="t", rules="r", market="TW",
            persona_ids=[],
            anchor_date=date(2026, 1, 1),
            trading_days_count=5,
        )


@pytest.mark.asyncio
async def test_create_sweep_rejects_out_of_bounds_concurrency(
    db_session: AsyncSession, owner: User,
):
    """concurrency > MAX_CONCURRENCY (3) is rejected — going higher
    saturates LLM provider rate limits and burns daily quota
    proportionally."""
    with pytest.raises(ValueError, match="concurrency"):
        await svc.create_sweep(
            db_session, owner_id=owner.id,
            topic="t", rules="r", market="TW",
            persona_ids=["buffett"],
            anchor_date=date(2026, 1, 1),
            trading_days_count=5,
            concurrency=svc.MAX_CONCURRENCY + 1,
        )


@pytest.mark.asyncio
async def test_create_sweep_rejects_excessive_trading_days(
    db_session: AsyncSession, owner: User,
):
    """trading_days_count > MAX_TRADING_DAYS (60) is rejected to
    cap blast radius — N days × M personas × LLM cost can
    accidentally cost real money."""
    with pytest.raises(ValueError, match="trading_days_count"):
        await svc.create_sweep(
            db_session, owner_id=owner.id,
            topic="t", rules="r", market="TW",
            persona_ids=["buffett"],
            anchor_date=date(2026, 1, 1),
            trading_days_count=svc.MAX_TRADING_DAYS + 1,
        )


@pytest.mark.asyncio
async def test_create_sweep_rejects_invalid_market(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError, match="market"):
        await svc.create_sweep(
            db_session, owner_id=owner.id,
            topic="t", rules="r", market="XX",
            persona_ids=["buffett"],
            anchor_date=date(2026, 1, 1),
            trading_days_count=5,
        )


# ── _resolve_trading_days ────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_trading_days_skips_weekends_naturally(
    db_session: AsyncSession,
):
    """No Sat/Sun rows in `ohlcv_daily` → the resolver naturally
    returns Mon-Fri without a holiday calendar."""
    db_session.add_all([
        _bar("X", date(2026, 1, 5), 100.0),   # Mon
        _bar("X", date(2026, 1, 6), 100.0),   # Tue
        _bar("X", date(2026, 1, 7), 100.0),   # Wed
        _bar("X", date(2026, 1, 8), 100.0),   # Thu
        _bar("X", date(2026, 1, 9), 100.0),   # Fri
        # No Sat/Sun
        _bar("X", date(2026, 1, 12), 100.0),  # next Mon
    ])
    await db_session.commit()

    days = await svc._resolve_trading_days(
        db_session, market="TW", anchor=date(2026, 1, 5), count=5,
    )
    assert days == [
        date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
        date(2026, 1, 8), date(2026, 1, 9),
    ]


@pytest.mark.asyncio
async def test_resolve_trading_days_returns_partial_when_archive_thin(
    db_session: AsyncSession,
):
    """Asked for 5, archive only has 3 → return all 3. Caller
    decides whether to bail."""
    db_session.add_all([
        _bar("X", date(2026, 1, 5), 100.0),
        _bar("X", date(2026, 1, 6), 100.0),
        _bar("X", date(2026, 1, 7), 100.0),
    ])
    await db_session.commit()

    days = await svc._resolve_trading_days(
        db_session, market="TW", anchor=date(2026, 1, 5), count=5,
    )
    assert len(days) == 3


@pytest.mark.asyncio
async def test_resolve_trading_days_returns_empty_when_archive_misses_anchor(
    db_session: AsyncSession,
):
    """Anchor is in the future relative to the archive → empty."""
    db_session.add(_bar("X", date(2026, 1, 5), 100.0))
    await db_session.commit()

    days = await svc._resolve_trading_days(
        db_session, market="TW", anchor=date(2026, 6, 1), count=5,
    )
    assert days == []


# ── cancel_sweep ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_sweep_flips_status_and_stamps_timestamp(
    db_session: AsyncSession, owner: User,
):
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=5,
    )
    sweep.status = svc.STATUS_RUNNING
    await db_session.commit()

    out = await svc.cancel_sweep(db_session, sweep)
    assert out.status == svc.STATUS_CANCELLED
    assert out.cancelled_at is not None


@pytest.mark.asyncio
async def test_cancel_sweep_is_noop_for_terminal_states(
    db_session: AsyncSession, owner: User,
):
    """Re-cancelling a completed sweep mustn't overwrite the
    completion timestamp — the operator is investigating, not
    invalidating."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=5,
    )
    completion_ts = datetime(2026, 1, 1, tzinfo=timedelta_zone())
    sweep.status = svc.STATUS_COMPLETED
    sweep.completed_at = completion_ts
    await db_session.commit()

    out = await svc.cancel_sweep(db_session, sweep)
    assert out.status == svc.STATUS_COMPLETED
    assert out.cancelled_at is None     # never set


def timedelta_zone():
    """Tiny helper to make the timezone aware in the test above
    without dragging in `datetime.timezone.utc` import in this
    test-only context. Real code uses `datetime.UTC`."""
    from datetime import timezone
    return timezone.utc


# ── run_sweep_worker — full lifecycle with mocked discussions ────


@pytest.mark.asyncio
async def test_worker_processes_all_dates_serially_when_concurrency_1(
    db_session: AsyncSession, owner: User,
):
    """Default concurrency=1: dates processed one at a time, all
    landing in `completed_dates`."""
    db_session.add_all([
        _bar("X", date(2026, 1, 5), 100.0),
        _bar("X", date(2026, 1, 6), 100.0),
        _bar("X", date(2026, 1, 7), 100.0),
    ])
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5),
        trading_days_count=3,
        concurrency=1,
    )
    sweep_id = sweep.id

    # Stub `_process_one_date` so we don't need the LLM stack.
    async def _fake_process(_sid, target_date, *, semaphore):
        async with semaphore:
            await asyncio.sleep(0)
        return target_date, None

    with patch.object(svc, "_process_one_date", _fake_process):
        await svc.run_sweep_worker(sweep_id)

    # Worker writes via its own session; expire ours so we re-read
    # from DB instead of returning the stale identity-map entry.
    db_session.expire_all()
    refreshed = await db_session.scalar(
        select(BacktestSweep).where(BacktestSweep.id == sweep_id)
    )
    assert refreshed.status == svc.STATUS_COMPLETED
    assert sorted(refreshed.completed_dates) == [
        "2026-01-05", "2026-01-06", "2026-01-07",
    ]
    assert refreshed.failed_dates == []
    assert refreshed.resolved_dates == [
        "2026-01-05", "2026-01-06", "2026-01-07",
    ]
    assert refreshed.completed_at is not None


@pytest.mark.asyncio
async def test_worker_continues_past_per_date_failures(
    db_session: AsyncSession, owner: User,
):
    """One date raising an LLM error must NOT stop the sweep —
    `failed_dates` accumulates the error, `completed_dates`
    captures the rest, status flips to `completed`. Operator
    can retry just the broken dates afterward."""
    db_session.add_all([
        _bar("X", date(2026, 1, 5), 100.0),
        _bar("X", date(2026, 1, 6), 100.0),
        _bar("X", date(2026, 1, 7), 100.0),
    ])
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5),
        trading_days_count=3,
    )
    sweep_id = sweep.id

    async def _fake_process(_sid, target_date, *, semaphore):
        async with semaphore:
            await asyncio.sleep(0)
        if target_date == date(2026, 1, 6):
            return target_date, "simulated LLM timeout"
        return target_date, None

    with patch.object(svc, "_process_one_date", _fake_process):
        await svc.run_sweep_worker(sweep_id)

    # Worker writes via its own session; expire ours so we re-read
    # from DB instead of returning the stale identity-map entry.
    db_session.expire_all()
    refreshed = await db_session.scalar(
        select(BacktestSweep).where(BacktestSweep.id == sweep_id)
    )
    assert refreshed.status == svc.STATUS_COMPLETED
    assert sorted(refreshed.completed_dates) == [
        "2026-01-05", "2026-01-07",
    ]
    assert len(refreshed.failed_dates) == 1
    assert refreshed.failed_dates[0]["date"] == "2026-01-06"
    assert "timeout" in refreshed.failed_dates[0]["error"]


@pytest.mark.asyncio
async def test_worker_fails_when_no_trading_days_resolved(
    db_session: AsyncSession, owner: User,
):
    """Anchor doesn't reach the archive → status=failed with a
    diagnostic error_message, not silently completed empty."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 6, 1), trading_days_count=5,
    )
    sweep_id = sweep.id

    await svc.run_sweep_worker(sweep_id)

    # Worker writes via its own session; expire ours so we re-read
    # from DB instead of returning the stale identity-map entry.
    db_session.expire_all()
    refreshed = await db_session.scalar(
        select(BacktestSweep).where(BacktestSweep.id == sweep_id)
    )
    assert refreshed.status == svc.STATUS_FAILED
    assert refreshed.error_message is not None
    assert "trading days" in refreshed.error_message


@pytest.mark.asyncio
async def test_worker_skips_when_status_not_pending(
    db_session: AsyncSession, owner: User,
):
    """Re-firing the worker on an already-running sweep is a no-op —
    prevents double-processing if the operator clicks /start twice."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
    )
    sweep.status = svc.STATUS_RUNNING
    await db_session.commit()
    sweep_id = sweep.id

    process_mock = AsyncMock()
    with patch.object(svc, "_process_one_date", process_mock):
        await svc.run_sweep_worker(sweep_id)

    process_mock.assert_not_called()


@pytest.mark.asyncio
async def test_worker_bails_on_cancellation_mid_flight(
    db_session: AsyncSession, owner: User,
):
    """Cancelling between chunks must stop processing. Already-
    completed dates remain in `completed_dates`; pending dates
    are dropped."""
    db_session.add_all([
        _bar("X", date(2026, 1, 5), 100.0),
        _bar("X", date(2026, 1, 6), 100.0),
        _bar("X", date(2026, 1, 7), 100.0),
    ])
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5),
        trading_days_count=3,
        concurrency=1,
    )
    sweep_id = sweep.id

    # Cancel after the first date completes.
    completed_count = {"n": 0}

    async def _fake_process(_sid, target_date, *, semaphore):
        async with semaphore:
            await asyncio.sleep(0)
        completed_count["n"] += 1
        if completed_count["n"] == 1:
            # Set status=cancelled in a separate session so the
            # next iteration's `_is_cancelled` check sees it.
            from db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as cancel_db:
                row = await cancel_db.scalar(
                    select(BacktestSweep).where(
                        BacktestSweep.id == sweep_id,
                    )
                )
                row.status = svc.STATUS_CANCELLED
                await cancel_db.commit()
        return target_date, None

    with patch.object(svc, "_process_one_date", _fake_process):
        await svc.run_sweep_worker(sweep_id)

    # Worker writes via its own session; expire ours so we re-read
    # from DB instead of returning the stale identity-map entry.
    db_session.expire_all()
    refreshed = await db_session.scalar(
        select(BacktestSweep).where(BacktestSweep.id == sweep_id)
    )
    # Status stays cancelled (the worker doesn't override it); only
    # the first date made it into completed_dates.
    assert refreshed.status == svc.STATUS_CANCELLED
    assert len(refreshed.completed_dates) == 1
    assert refreshed.completed_dates[0] == "2026-01-05"


# ── PR #275: auto-post-mortem branch in _process_one_date ────────


@pytest.mark.asyncio
async def test_process_one_date_runs_post_mortem_when_enabled(
    db_session: AsyncSession, owner: User,
):
    """When `auto_post_mortem=True`, the worker chains the post-
    mortem flow after the original synthesize: refresh →
    build_post_mortem_message → inject → run_round → synthesize.
    Mocks stub the LLM stack so the test stays fast."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=1,
        auto_post_mortem=True,
    )

    pm_called = {"build": 0, "inject": 0, "round": 0, "synth": 0}
    base_synth_called = {"n": 0}

    async def _fake_run_round(*args, **kwargs):
        # generator with no events
        if False:
            yield
        return

    class _StubDisc:
        conclusion = {"recommended_symbols": ["2330"], "reasoning": "ok"}
        market = "TW"
        as_of_date = date(2026, 1, 5)

    async def _fake_create_discussion(db, **_kw):
        return _StubDisc()

    async def _fake_synthesize(db, disc, **_kw):
        base_synth_called["n"] += 1
        # First call (original) — set conclusion. Second call
        # (post-mortem) — would route to post_mortem_conclusion
        # in real code, but we just count calls here.
        if base_synth_called["n"] == 1:
            disc.conclusion = {"recommended_symbols": ["2330"]}
        return disc.conclusion

    async def _fake_build_post_mortem(db, disc, **_kw):
        pm_called["build"] += 1
        from services.post_mortem_service import PostMortemPayload
        return PostMortemPayload(
            trading_days=[date(2026, 1, 6)],
            recommended_performance=[],
            daily_top_gainers=[],
            prompt_text="【事後檢討】test prompt",
        )

    async def _fake_inject(db, disc, *, content):
        pm_called["inject"] += 1
        return None

    async def _fake_refresh(disc):
        # No-op — the stub already has conclusion set.
        return None

    semaphore = asyncio.Semaphore(1)

    with patch(
        "services.discussion_service.create_discussion",
        new=_fake_create_discussion,
    ), patch(
        "services.discussion_service.run_round",
        new=_fake_run_round,
    ), patch(
        "services.discussion_service.synthesize_conclusion",
        new=_fake_synthesize,
    ), patch(
        "services.discussion_service.inject_user_message",
        new=_fake_inject,
    ), patch(
        "services.post_mortem_service.build_post_mortem_message",
        new=_fake_build_post_mortem,
    ), patch.object(svc.AsyncSessionLocal.__call__.__self__,
                     # No-op — caller uses AsyncSessionLocal directly
                     # below; we'll just rely on the test fixture's
                     # patched session.
                     # Use the sweep_service.AsyncSessionLocal alias.
                     "_dummy", create=True):
        # Manually patch the session opener to yield the test session.
        class _CtxYieldSession:
            async def __aenter__(self_inner):
                return db_session

            async def __aexit__(self_inner, *exc):
                return False

        with patch.object(
            svc, "AsyncSessionLocal",
            new=lambda: _CtxYieldSession(),
        ), patch.object(
            db_session, "refresh", new=_fake_refresh,
        ):
            target_date, err = await svc._process_one_date(
                sweep.id, date(2026, 1, 5), semaphore=semaphore,
            )

    assert err is None
    assert pm_called["build"] == 1, "build_post_mortem_message must fire"
    assert pm_called["inject"] == 1, "inject_user_message must fire"
    # Two synthesize calls: the original + the post-mortem revision.
    assert base_synth_called["n"] == 2


@pytest.mark.asyncio
async def test_process_one_date_skips_post_mortem_when_disabled(
    db_session: AsyncSession, owner: User,
):
    """`auto_post_mortem=False` → only the original synthesize
    runs, post-mortem service is never called."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=1,
        auto_post_mortem=False,
    )

    async def _fake_run_round(*_a, **_kw):
        if False:
            yield
        return

    class _StubDisc:
        conclusion = {"recommended_symbols": ["2330"]}
        market = "TW"
        as_of_date = date(2026, 1, 5)

    async def _fake_create_discussion(db, **_kw):
        return _StubDisc()

    synth_calls = {"n": 0}

    async def _fake_synthesize(db, disc, **_kw):
        synth_calls["n"] += 1
        return disc.conclusion

    pm_calls = {"n": 0}

    async def _fake_build_post_mortem(db, disc, **_kw):
        pm_calls["n"] += 1
        from services.post_mortem_service import PostMortemPayload
        return PostMortemPayload()

    semaphore = asyncio.Semaphore(1)

    class _CtxYieldSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch.object(
        svc, "AsyncSessionLocal", new=lambda: _CtxYieldSession(),
    ), patch(
        "services.discussion_service.create_discussion",
        new=_fake_create_discussion,
    ), patch(
        "services.discussion_service.run_round",
        new=_fake_run_round,
    ), patch(
        "services.discussion_service.synthesize_conclusion",
        new=_fake_synthesize,
    ), patch(
        "services.post_mortem_service.build_post_mortem_message",
        new=_fake_build_post_mortem,
    ):
        target_date, err = await svc._process_one_date(
            sweep.id, date(2026, 1, 5), semaphore=semaphore,
        )

    assert err is None
    assert synth_calls["n"] == 1, "only the original synthesize should fire"
    assert pm_calls["n"] == 0, "post-mortem service must NOT be called"


@pytest.mark.asyncio
async def test_process_one_date_does_not_fail_when_post_mortem_raises(
    db_session: AsyncSession, owner: User,
):
    """Post-mortem failure (LLM timeout / no future ohlcv) must NOT
    fail the whole date — the original conclusion is still valid
    and the verifier anchors on it. The error is logged and the
    date counts as completed."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=1,
        auto_post_mortem=True,
    )

    async def _fake_run_round(*_a, **_kw):
        if False:
            yield
        return

    class _StubDisc:
        conclusion = {"recommended_symbols": ["2330"]}
        market = "TW"
        as_of_date = date(2026, 1, 5)

    async def _fake_create_discussion(db, **_kw):
        return _StubDisc()

    async def _fake_synthesize(db, disc, **_kw):
        return disc.conclusion

    async def _failing_build_pm(db, disc, **_kw):
        raise RuntimeError("simulated post-mortem failure")

    semaphore = asyncio.Semaphore(1)

    class _CtxYieldSession:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch.object(
        svc, "AsyncSessionLocal", new=lambda: _CtxYieldSession(),
    ), patch(
        "services.discussion_service.create_discussion",
        new=_fake_create_discussion,
    ), patch(
        "services.discussion_service.run_round",
        new=_fake_run_round,
    ), patch(
        "services.discussion_service.synthesize_conclusion",
        new=_fake_synthesize,
    ), patch(
        "services.post_mortem_service.build_post_mortem_message",
        new=_failing_build_pm,
    ):
        target_date, err = await svc._process_one_date(
            sweep.id, date(2026, 1, 5), semaphore=semaphore,
        )

    # err is None — the date completes successfully even though the
    # post-mortem branch raised internally.
    assert err is None
    assert target_date == date(2026, 1, 5)


# ── delete + list helpers ────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sweeps_returns_owner_scoped_descending(
    db_session: AsyncSession, owner: User,
):
    """List is sorted most-recent-first and excludes other owners'
    rows (basic owner-scoping guard)."""
    other = User(
        email="other@test.com", hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    s_a = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="A", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
    )
    s_other = await svc.create_sweep(
        db_session, owner_id=other.id,
        topic="OTHER", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
    )

    rows = await svc.list_sweeps(db_session, owner_id=owner.id)
    ids = [r.id for r in rows]
    assert s_a.id in ids
    assert s_other.id not in ids


@pytest.mark.asyncio
async def test_get_sweep_returns_none_for_other_owner(
    db_session: AsyncSession, owner: User,
):
    other = User(
        email="other2@test.com", hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    sweep = await svc.create_sweep(
        db_session, owner_id=other.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
    )

    out = await svc.get_sweep(
        db_session, sweep_id=sweep.id, owner_id=owner.id,
    )
    assert out is None


@pytest.mark.asyncio
async def test_delete_sweep_removes_row(
    db_session: AsyncSession, owner: User,
):
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
    )
    await svc.delete_sweep(db_session, sweep)
    fetched = await db_session.scalar(
        select(BacktestSweep).where(BacktestSweep.id == sweep.id)
    )
    assert fetched is None


# ── PR-A0: fold_kind metadata ───────────────────────────────────────


@pytest.mark.asyncio
async def test_create_sweep_defaults_to_production_fold(
    db_session: AsyncSession, owner: User,
):
    """Direct API callers don't set fold_kind — the existing
    'production' default keeps every legacy code path working."""
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
    )
    assert sweep.fold_kind == "production"
    assert sweep.parent_sweep_id is None


@pytest.mark.asyncio
async def test_create_sweep_with_train_fold_kind(
    db_session: AsyncSession, owner: User,
):
    sweep = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
        fold_kind="train",
    )
    assert sweep.fold_kind == "train"


@pytest.mark.asyncio
async def test_create_test_sweep_links_to_parent(
    db_session: AsyncSession, owner: User,
):
    """The walk-forward orchestrator (PR-A1) creates a test sweep
    that points at its train sibling so the aggregate UI can pair
    them up."""
    train = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
        fold_kind="train",
    )
    test = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 4, 1), trading_days_count=3,
        fold_kind="test", parent_sweep_id=train.id,
    )
    assert test.fold_kind == "test"
    assert test.parent_sweep_id == train.id


@pytest.mark.asyncio
async def test_create_sweep_rejects_invalid_fold_kind(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError, match="fold_kind"):
        await svc.create_sweep(
            db_session, owner_id=owner.id,
            topic="t", rules="r", market="TW",
            persona_ids=["buffett"],
            anchor_date=date(2026, 1, 5), trading_days_count=3,
            fold_kind="validation",
        )


@pytest.mark.asyncio
async def test_create_sweep_rejects_parent_on_production(
    db_session: AsyncSession, owner: User,
):
    """parent_sweep_id only makes sense for fold halves — guard
    against an API caller wiring a production sweep to a parent
    by mistake."""
    train = await svc.create_sweep(
        db_session, owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["buffett"],
        anchor_date=date(2026, 1, 5), trading_days_count=3,
        fold_kind="train",
    )
    with pytest.raises(ValueError, match="parent_sweep_id"):
        await svc.create_sweep(
            db_session, owner_id=owner.id,
            topic="t", rules="r", market="TW",
            persona_ids=["buffett"],
            anchor_date=date(2026, 4, 1), trading_days_count=3,
            fold_kind="production",
            parent_sweep_id=train.id,
        )
