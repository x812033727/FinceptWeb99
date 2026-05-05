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
    texts = [l["lesson_text"] for l in payload["lessons"]]
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
