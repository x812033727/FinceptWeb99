"""Tests for `services.sweep_aggregate_service` (PR-B)."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion, DiscussionTurn
from models.discussion_lesson import DiscussionLesson
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import sweep_aggregate_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        email=f"agg-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


def _make_sweep(
    owner_id: uuid.UUID,
    *,
    persona_ids: list[str] | None = None,
    market: str = "TW",
    strategy_id: uuid.UUID | None = None,
    completed: list[str] | None = None,
    failed: list[dict] | None = None,
) -> BacktestSweep:
    return BacktestSweep(
        owner_id=owner_id,
        topic="t", rules="r", market=market,
        persona_ids=persona_ids or ["bull", "bear"],
        anchor_date=date(2026, 1, 5),
        trading_days_count=5,
        rounds_per_discussion=1,
        concurrency=1,
        auto_post_mortem=True,
        strategy_id=strategy_id,
        resolved_dates=completed or [],
        completed_dates=completed or [],
        failed_dates=failed or [],
    )


def _make_disc(
    *, owner_id: uuid.UUID, sweep_id: uuid.UUID,
    persona_ids: list[str] | None = None,
    as_of: date = date(2026, 1, 5),
    verdict: str | None = None,
    opens: dict[str, float] | None = None,
    daily: dict[str, list[float | None]] | None = None,
) -> Discussion:
    return Discussion(
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=persona_ids or ["bull", "bear"],
        market="TW",
        status="done",
        current_round=1,
        as_of_date=as_of,
        sweep_id=sweep_id,
        verdict=verdict,
        day1_open_prices=opens,
        daily_close_prices=daily,
    )


# ── empty / smoke ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_sweep_returns_zero_payload(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(owner.id)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert payload["scope"] == "sweep"
    assert payload["discussions_total"] == 0
    assert payload["win_rate"] is None
    assert payload["avg_pnl_pct"] == [None] * svc.WINDOW_DAYS
    # roster still present in per_persona for both personas
    assert {p["persona_id"] for p in payload["per_persona"]} == {"bull", "bear"}
    for p in payload["per_persona"]:
        assert p["discussions_count"] == 0
        assert p["win_count"] == 0
        assert p["hit_rate"] is None


@pytest.mark.asyncio
async def test_aggregate_includes_verdict_and_pnl(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-06"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # Discussion 1: win, 2330 +5% / +10% / +15% / +12% / +20% (per day vs day1_open)
    disc1 = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id,
        as_of=date(2026, 1, 5),
        verdict="win",
        opens={"2330": 100.0},
        daily={"2330": [105.0, 110.0, 115.0, 112.0, 120.0]},
    )
    # Discussion 2: loss, 2454 -2% on D1 only, rest NULL (unresolved)
    disc2 = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id,
        as_of=date(2026, 1, 6),
        verdict="loss",
        opens={"2454": 200.0},
        daily={"2454": [196.0, None, None, None, None]},
    )
    db_session.add_all([disc1, disc2])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert payload["discussions_total"] == 2
    assert payload["verdict_counts"] == {
        "win": 1, "loss": 1, "unverifiable": 0, "pending": 0,
    }
    assert payload["win_rate"] == 0.5

    # D1 = avg(+5%, -2%) = +1.5%; D2 = +10% (only disc1 contributes); ...
    assert payload["avg_pnl_pct"][0] == pytest.approx(0.015)
    assert payload["avg_pnl_pct"][1] == pytest.approx(0.10)
    assert payload["avg_pnl_pct"][4] == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_per_persona_hit_rate_attribution(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(
        owner.id, persona_ids=["bull", "bear", "neutral"],
    )
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # Three discussions, all use bull+bear+neutral roster.
    # Wins: 2/3.
    for i, verdict in enumerate(["win", "win", "loss"]):
        d = _make_disc(
            owner_id=owner.id, sweep_id=sweep.id,
            persona_ids=["bull", "bear", "neutral"],
            as_of=date(2026, 1, 5 + i),
            verdict=verdict,
        )
        db_session.add(d)
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    by_id = {p["persona_id"]: p for p in payload["per_persona"]}
    for pid in ("bull", "bear", "neutral"):
        assert by_id[pid]["discussions_count"] == 3
        assert by_id[pid]["win_count"] == 2
        assert by_id[pid]["hit_rate"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_persona_turn_counts_populate(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(owner.id)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)
    disc = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id, verdict="win",
    )
    db_session.add(disc)
    await db_session.commit()
    await db_session.refresh(disc)

    db_session.add_all([
        DiscussionTurn(discussion_id=disc.id, round=1, turn_index=0,
                       persona_id="bull", stance="agree", content="x"),
        DiscussionTurn(discussion_id=disc.id, round=1, turn_index=1,
                       persona_id="bull", stance="supplement", content="y"),
        DiscussionTurn(discussion_id=disc.id, round=1, turn_index=2,
                       persona_id="bear", stance="dissent", content="z"),
    ])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    by_id = {p["persona_id"]: p for p in payload["per_persona"]}
    # agree + supplement counted together
    assert by_id["bull"]["agree_turn_count"] == 2
    assert by_id["bull"]["dissent_turn_count"] == 0
    assert by_id["bear"]["agree_turn_count"] == 0
    assert by_id["bear"]["dissent_turn_count"] == 1


@pytest.mark.asyncio
async def test_lessons_filtered_by_owner_market_and_window(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-07"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # In-window lesson — should appear.
    db_session.add(DiscussionLesson(
        discussion_id=uuid.uuid4(),
        owner_user_id=owner.id,
        market="TW",
        as_of_date=date(2026, 1, 6),
        category="missed_winner",
        lesson_text="忽略了 2330 的高成交量",
        lesson_text_hash="h1",
        related_symbols=["2330"],
        missed_winners=[],
        created_at=datetime(2026, 1, 8, tzinfo=UTC),
    ))
    # Out-of-window lesson — should NOT appear.
    db_session.add(DiscussionLesson(
        discussion_id=uuid.uuid4(),
        owner_user_id=owner.id,
        market="TW",
        as_of_date=date(2025, 12, 1),
        category="risk",
        lesson_text="冷門",
        lesson_text_hash="h2",
        related_symbols=[],
        missed_winners=[],
    ))
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    texts = [item["lesson_text"] for item in payload["lessons"]]
    assert texts == ["忽略了 2330 的高成交量"]


# ── strategy aggregate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregate_strategy_unions_sweeps(
    db_session: AsyncSession, owner: User,
):
    tmpl = DiscussionStrategyTemplate(
        owner_id=owner.id, name="t1",
        topic="t", rules="r", market="TW",
        persona_ids=["bull"], default_rounds=1,
        default_concurrency=1, default_auto_post_mortem=True,
    )
    db_session.add(tmpl)
    await db_session.commit()
    await db_session.refresh(tmpl)

    s1 = _make_sweep(owner.id, strategy_id=tmpl.id, persona_ids=["bull"])
    s2 = _make_sweep(owner.id, strategy_id=tmpl.id, persona_ids=["bear"])
    db_session.add_all([s1, s2])
    await db_session.commit()
    await db_session.refresh(s1)
    await db_session.refresh(s2)

    db_session.add_all([
        _make_disc(
            owner_id=owner.id, sweep_id=s1.id,
            persona_ids=["bull"], verdict="win",
        ),
        _make_disc(
            owner_id=owner.id, sweep_id=s2.id,
            persona_ids=["bear"], verdict="loss",
        ),
    ])
    await db_session.commit()

    payload = await svc.aggregate_strategy(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    assert payload["scope"] == "strategy"
    assert payload["sweep_count"] == 2
    assert payload["discussions_total"] == 2
    assert payload["verdict_counts"]["win"] == 1
    assert payload["verdict_counts"]["loss"] == 1
    persona_ids = {p["persona_id"] for p in payload["per_persona"]}
    assert persona_ids == {"bull", "bear"}


@pytest.mark.asyncio
async def test_aggregate_strategy_returns_empty_when_no_sweeps(
    db_session: AsyncSession, owner: User,
):
    payload = await svc.aggregate_strategy(
        db_session, owner_id=owner.id, strategy_id=uuid.uuid4(),
    )
    assert payload["sweep_count"] == 0
    assert payload["discussions_total"] == 0
    assert payload["per_persona"] == []


# ── PR-A0: fold_kind passes through to aggregate payload ──────────


@pytest.mark.asyncio
async def test_aggregate_sweep_exposes_fold_metadata(
    db_session: AsyncSession, owner: User,
):
    train = _make_sweep(owner.id)
    train.fold_kind = "train"
    db_session.add(train)
    await db_session.commit()
    await db_session.refresh(train)

    test = _make_sweep(owner.id)
    test.fold_kind = "test"
    test.parent_sweep_id = train.id
    db_session.add(test)
    await db_session.commit()
    await db_session.refresh(test)

    payload_train = await svc.aggregate_sweep(db_session, train)
    assert payload_train["fold_kind"] == "train"
    assert payload_train["parent_sweep_id"] is None

    payload_test = await svc.aggregate_sweep(db_session, test)
    assert payload_test["fold_kind"] == "test"
    assert payload_test["parent_sweep_id"] == str(train.id)


@pytest.mark.asyncio
async def test_aggregate_sweep_legacy_sweeps_default_to_production(
    db_session: AsyncSession, owner: User,
):
    """Pre-PR-A0 sweep rows already in the DB get fold_kind defaulted
    to 'production' by the migration's server_default. The aggregate
    output should surface that without erroring."""
    sweep = _make_sweep(owner.id)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert payload["fold_kind"] == "production"
    assert payload["parent_sweep_id"] is None


# ── PR-C1: Brier score + reliability rolled up at sweep level ────────


@pytest.mark.asyncio
async def test_aggregate_includes_brier_when_discussions_resolved(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-06"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # Discussion A: confidence 0.8, outcome 1, loss = 0.04
    a = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id,
        verdict="win",
        opens={"2330": 100.0},
        daily={"2330": [110.0, 105.0, 102.0, 108.0, 109.0]},
    )
    a.brier_score = 0.04
    a.outcome_vector = [
        {"symbol": "2330", "confidence": 0.8, "outcome_binary": 1},
    ]
    # Discussion B: confidence 0.7, outcome 0, loss = 0.49
    b = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id,
        verdict="loss",
        opens={"2454": 100.0},
        daily={"2454": [101.0, 102.0, 100.5, 100.0, 99.5]},
    )
    b.brier_score = 0.49
    b.outcome_vector = [
        {"symbol": "2454", "confidence": 0.7, "outcome_binary": 0},
    ]
    db_session.add_all([a, b])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    # weighted average: (0.04*1 + 0.49*1) / (1+1) = 0.265
    assert payload["brier_score"] == pytest.approx(0.265, abs=1e-6)
    assert payload["brier_samples"] == 2
    # Reliability buckets always have 10 entries (with empty placeholders)
    assert len(payload["reliability"]) == 10


@pytest.mark.asyncio
async def test_aggregate_brier_weighted_by_sample_count(
    db_session: AsyncSession, owner: User,
):
    """Discussion with more recommendations should pull the average
    proportionally more than a 1-pick discussion."""
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-06"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # A has 4 picks all loss=0.04 → discussion brier=0.04, samples=4
    a = _make_disc(owner_id=owner.id, sweep_id=sweep.id, verdict="win")
    a.brier_score = 0.04
    a.outcome_vector = [
        {"symbol": f"S{i}", "confidence": 0.8, "outcome_binary": 1}
        for i in range(4)
    ]
    # B has 1 pick loss=0.81 → discussion brier=0.81, samples=1
    b = _make_disc(owner_id=owner.id, sweep_id=sweep.id, verdict="loss")
    b.brier_score = 0.81
    b.outcome_vector = [
        {"symbol": "X", "confidence": 0.9, "outcome_binary": 0},
    ]
    db_session.add_all([a, b])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    # weighted: (0.04*4 + 0.81*1) / (4+1) = 0.97 / 5 = 0.194
    assert payload["brier_score"] == pytest.approx(0.194, abs=1e-6)
    assert payload["brier_samples"] == 5


@pytest.mark.asyncio
async def test_aggregate_brier_skips_unresolved_discussions(
    db_session: AsyncSession, owner: User,
):
    """Mix of resolved + pending — only the resolved one contributes
    to brier; unresolved rows shouldn't poison the average."""
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-06"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    a = _make_disc(owner_id=owner.id, sweep_id=sweep.id, verdict="win")
    a.brier_score = 0.04
    a.outcome_vector = [
        {"symbol": "S", "confidence": 0.8, "outcome_binary": 1},
    ]
    pending = _make_disc(owner_id=owner.id, sweep_id=sweep.id)
    # leave pending.brier_score = None
    db_session.add_all([a, pending])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert payload["brier_score"] == pytest.approx(0.04, abs=1e-6)
    assert payload["brier_samples"] == 1


@pytest.mark.asyncio
async def test_aggregate_brier_null_when_no_resolved_discussions(
    db_session: AsyncSession, owner: User,
):
    sweep = _make_sweep(owner.id)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)
    pending = _make_disc(owner_id=owner.id, sweep_id=sweep.id)
    db_session.add(pending)
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert payload["brier_score"] is None
    assert payload["brier_samples"] == 0
    assert payload["reliability"] == []


@pytest.mark.asyncio
async def test_empty_sweep_includes_brier_keys_in_payload(
    db_session: AsyncSession, owner: User,
):
    """Empty payload still has the Brier keys so the dashboard can
    `payload.brier_score ?? "n/a"` without `undefined` checks."""
    sweep = _make_sweep(owner.id)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert "brier_score" in payload
    assert "brier_samples" in payload
    assert "calibrated_brier_score" in payload
    assert "calibrated_brier_samples" in payload
    assert "reliability" in payload


# ── PR-C2 follow-up: calibrated_brier sweep-level aggregate ─────────


@pytest.mark.asyncio
async def test_aggregate_includes_calibrated_brier_when_present(
    db_session: AsyncSession, owner: User,
):
    """Two discussions, both fully calibrated. Sweep-level
    calibrated_brier is the sample-weighted mean."""
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-06"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    a = _make_disc(owner_id=owner.id, sweep_id=sweep.id, verdict="win")
    a.brier_score = 0.04
    a.calibrated_brier_score = 0.02
    a.outcome_vector = [
        {
            "symbol": "A", "confidence": 0.8,
            "calibrated_confidence": 0.6, "outcome_binary": 1,
        },
    ]
    b = _make_disc(owner_id=owner.id, sweep_id=sweep.id, verdict="loss")
    b.brier_score = 0.49
    b.calibrated_brier_score = 0.16
    b.outcome_vector = [
        {
            "symbol": "B", "confidence": 0.7,
            "calibrated_confidence": 0.4, "outcome_binary": 0,
        },
    ]
    db_session.add_all([a, b])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    # raw: (0.04 + 0.49) / 2 = 0.265
    assert payload["brier_score"] == pytest.approx(0.265, abs=1e-6)
    # calibrated: (0.02 + 0.16) / 2 = 0.09 — the curve is helping
    assert payload["calibrated_brier_score"] == pytest.approx(
        0.09, abs=1e-6,
    )
    assert payload["brier_samples"] == 2
    assert payload["calibrated_brier_samples"] == 2


@pytest.mark.asyncio
async def test_aggregate_calibrated_brier_skips_uncalibrated_discussions(
    db_session: AsyncSession, owner: User,
):
    """Only discussions with calibrated_brier_score contribute;
    cold-start / pre-PR-C2 discussions stay out of the calibrated
    rollup but still count toward the raw brier."""
    sweep = _make_sweep(owner.id, completed=["2026-01-05", "2026-01-06"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    fully_calibrated = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id, verdict="win",
    )
    fully_calibrated.brier_score = 0.04
    fully_calibrated.calibrated_brier_score = 0.02
    fully_calibrated.outcome_vector = [
        {
            "symbol": "A", "confidence": 0.8,
            "calibrated_confidence": 0.6, "outcome_binary": 1,
        },
    ]

    raw_only = _make_disc(
        owner_id=owner.id, sweep_id=sweep.id, verdict="loss",
    )
    raw_only.brier_score = 0.49
    # No calibrated_brier_score — pre-PR-C2 row.
    raw_only.outcome_vector = [
        {"symbol": "B", "confidence": 0.7, "outcome_binary": 0},
    ]

    db_session.add_all([fully_calibrated, raw_only])
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    # raw averages over both
    assert payload["brier_score"] == pytest.approx(0.265, abs=1e-6)
    assert payload["brier_samples"] == 2
    # calibrated only counts the fully calibrated discussion
    assert payload["calibrated_brier_score"] == pytest.approx(
        0.02, abs=1e-6,
    )
    assert payload["calibrated_brier_samples"] == 1


# ── fetch_strategy_brier_history (audit Workflow Win #1) ────────────


@pytest.mark.asyncio
async def test_brier_history_empty_when_no_sweeps(
    db_session: AsyncSession, owner: User,
):
    out = await svc.fetch_strategy_brier_history(
        db_session,
        owner_id=owner.id,
        strategy_id=uuid.uuid4(),
        window_days=30,
    )
    assert out == []


@pytest.mark.asyncio
async def test_brier_history_returns_one_point_per_sweep(
    db_session: AsyncSession, owner: User,
):
    """Two completed sweeps with resolved discussions = two
    points, ordered by completion time ascending."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )
    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="trend test",
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        default_rounds=1,
        default_concurrency=1,
        default_auto_post_mortem=False,
    )
    db_session.add(tmpl)
    await db_session.commit()

    older = _make_sweep(owner.id, completed=["2026-04-01"])
    older.strategy_id = tmpl.id
    older.status = "completed"
    older.completed_at = _dt.now(_UTC) - _td(days=20)
    newer = _make_sweep(owner.id, completed=["2026-05-01"])
    newer.strategy_id = tmpl.id
    newer.status = "completed"
    newer.completed_at = _dt.now(_UTC) - _td(days=2)
    db_session.add_all([older, newer])
    await db_session.commit()
    await db_session.refresh(older)
    await db_session.refresh(newer)

    older_disc = _make_disc(
        owner_id=owner.id, sweep_id=older.id, verdict="loss",
    )
    older_disc.brier_score = 0.30
    older_disc.calibrated_brier_score = 0.28
    older_disc.outcome_vector = [
        {"symbol": "X", "confidence": 0.7, "outcome_binary": 0},
    ]
    newer_disc = _make_disc(
        owner_id=owner.id, sweep_id=newer.id, verdict="win",
    )
    newer_disc.brier_score = 0.10
    newer_disc.calibrated_brier_score = 0.08
    newer_disc.outcome_vector = [
        {"symbol": "Y", "confidence": 0.7, "outcome_binary": 1},
    ]
    db_session.add_all([older_disc, newer_disc])
    await db_session.commit()

    points = await svc.fetch_strategy_brier_history(
        db_session,
        owner_id=owner.id,
        strategy_id=tmpl.id,
        window_days=90,
    )
    assert len(points) == 2
    # Ordered ascending — older sweep first
    assert points[0]["raw_brier"] == pytest.approx(0.30, abs=1e-6)
    assert points[1]["raw_brier"] == pytest.approx(0.10, abs=1e-6)
    assert points[0]["calibrated_brier"] == pytest.approx(0.28, abs=1e-6)
    assert points[1]["calibrated_brier"] == pytest.approx(0.08, abs=1e-6)
    # Sweep IDs preserved as strings
    assert points[0]["sweep_id"] == str(older.id)
    assert points[1]["sweep_id"] == str(newer.id)


@pytest.mark.asyncio
async def test_brier_history_excludes_pending_sweeps(
    db_session: AsyncSession, owner: User,
):
    """Sweeps that are still running shouldn't appear — would
    show as flat lines with no datapoint and confuse the chart."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )
    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner.id, name="t",
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=False,
    )
    db_session.add(tmpl)
    await db_session.commit()

    running = _make_sweep(owner.id)
    running.strategy_id = tmpl.id
    running.status = "running"   # still in-flight
    running.completed_at = None
    completed = _make_sweep(owner.id, completed=["2026-05-01"])
    completed.strategy_id = tmpl.id
    completed.status = "completed"
    completed.completed_at = _dt.now(_UTC) - _td(days=1)
    db_session.add_all([running, completed])
    await db_session.commit()
    await db_session.refresh(running)
    await db_session.refresh(completed)

    completed_disc = _make_disc(
        owner_id=owner.id, sweep_id=completed.id, verdict="win",
    )
    completed_disc.brier_score = 0.12
    completed_disc.outcome_vector = [
        {"symbol": "X", "confidence": 0.7, "outcome_binary": 1},
    ]
    db_session.add(completed_disc)
    await db_session.commit()

    points = await svc.fetch_strategy_brier_history(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id, window_days=90,
    )
    assert len(points) == 1
    assert points[0]["sweep_id"] == str(completed.id)


@pytest.mark.asyncio
async def test_brier_history_respects_window_cutoff(
    db_session: AsyncSession, owner: User,
):
    """Sweeps that completed >window_days ago must NOT appear.
    Operator's trend chart cares about recent direction, not
    ancient history."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )
    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner.id, name="t",
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=False,
    )
    db_session.add(tmpl)
    await db_session.commit()

    ancient = _make_sweep(owner.id, completed=["2024-01-01"])
    ancient.strategy_id = tmpl.id
    ancient.status = "completed"
    ancient.completed_at = _dt.now(_UTC) - _td(days=400)
    recent = _make_sweep(owner.id, completed=["2026-05-01"])
    recent.strategy_id = tmpl.id
    recent.status = "completed"
    recent.completed_at = _dt.now(_UTC) - _td(days=2)
    db_session.add_all([ancient, recent])
    await db_session.commit()
    await db_session.refresh(ancient)
    await db_session.refresh(recent)
    ancient_disc = _make_disc(
        owner_id=owner.id, sweep_id=ancient.id, verdict="loss",
    )
    ancient_disc.brier_score = 0.40
    ancient_disc.outcome_vector = [
        {"symbol": "X", "confidence": 0.7, "outcome_binary": 0},
    ]
    recent_disc = _make_disc(
        owner_id=owner.id, sweep_id=recent.id, verdict="win",
    )
    recent_disc.brier_score = 0.10
    recent_disc.outcome_vector = [
        {"symbol": "Y", "confidence": 0.7, "outcome_binary": 1},
    ]
    db_session.add_all([ancient_disc, recent_disc])
    await db_session.commit()

    # 30-day window — only the recent sweep qualifies
    points = await svc.fetch_strategy_brier_history(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id, window_days=30,
    )
    assert len(points) == 1
    assert points[0]["sweep_id"] == str(recent.id)

    # 730-day window — both qualify
    points = await svc.fetch_strategy_brier_history(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id, window_days=730,
    )
    assert len(points) == 2


@pytest.mark.asyncio
async def test_aggregate_calibrated_brier_null_when_none(
    db_session: AsyncSession, owner: User,
):
    """Sweep with no calibrated discussions at all — calibrated_
    brier_score is NULL so the dashboard renders "n/a" instead
    of misleading 0."""
    sweep = _make_sweep(owner.id, completed=["2026-01-05"])
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    a = _make_disc(owner_id=owner.id, sweep_id=sweep.id, verdict="win")
    a.brier_score = 0.04
    a.outcome_vector = [
        {"symbol": "A", "confidence": 0.8, "outcome_binary": 1},
    ]
    db_session.add(a)
    await db_session.commit()

    payload = await svc.aggregate_sweep(db_session, sweep)
    assert payload["brier_score"] == pytest.approx(0.04, abs=1e-6)
    assert payload["calibrated_brier_score"] is None
    assert payload["calibrated_brier_samples"] == 0
