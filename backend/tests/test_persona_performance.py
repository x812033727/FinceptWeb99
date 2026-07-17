"""PR-5b: tests for `compute_persona_performance` — the leaderboard
aggregator over participation / win attribution / weight stats.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion, DiscussionTurn
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_version import StrategyVersion
from models.user import User, UserRole
from services import persona_performance_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"pp-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_discussion(
    db: AsyncSession, owner_id: uuid.UUID,
    *,
    persona_ids: list[str], verdict: str | None = None,
    sweep_id: uuid.UUID | None = None,
    days_old: int = 0,
    market: str = "TW",
) -> Discussion:
    now = datetime.now(UTC) - timedelta(days=days_old)
    d = Discussion(
        id=uuid4(), owner_id=owner_id, sweep_id=sweep_id,
        topic="x", rules="", market=market,
        persona_ids=persona_ids,
        status="done", current_round=1,
        verdict=verdict,
        created_at=now, updated_at=now,
    )
    db.add(d)
    await db.flush()
    for i, pid in enumerate(persona_ids):
        db.add(DiscussionTurn(
            discussion_id=d.id, round=1, turn_index=i,
            persona_id=pid, stance="agree", content="x",
        ))
    await db.commit()
    return d


# ── empty / cold-start ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_when_no_discussions(
    db_session: AsyncSession, owner: User,
):
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    assert out == []


# ── participation + win attribution ──────────────────────────────


@pytest.mark.asyncio
async def test_participation_counts_per_persona(
    db_session: AsyncSession, owner: User,
):
    """Two discussions with overlapping rosters → buffett 2,
    lynch 1, soros 1."""
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett", "lynch"],
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett", "soros"],
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    by_id = {r["persona_id"]: r for r in out}
    assert by_id["buffett"]["participation_count"] == 2
    assert by_id["lynch"]["participation_count"] == 1
    assert by_id["soros"]["participation_count"] == 1


@pytest.mark.asyncio
async def test_win_attribution_only_for_win_verdict(
    db_session: AsyncSession, owner: User,
):
    """3 discussions: 2 win + 1 loss. buffett in all 3 → 2/3
    win rate. lynch only in the loss → 0/1."""
    await _make_discussion(
        db_session, owner.id,
        persona_ids=["buffett", "soros"], verdict="win",
    )
    await _make_discussion(
        db_session, owner.id,
        persona_ids=["buffett"], verdict="win",
    )
    await _make_discussion(
        db_session, owner.id,
        persona_ids=["buffett", "lynch"], verdict="loss",
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    by_id = {r["persona_id"]: r for r in out}
    assert by_id["buffett"]["win_attribution_count"] == 2
    assert by_id["buffett"]["win_attribution_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert by_id["lynch"]["win_attribution_count"] == 0
    assert by_id["lynch"]["win_attribution_rate"] == 0.0


@pytest.mark.asyncio
async def test_user_input_persona_excluded(
    db_session: AsyncSession, owner: User,
):
    """The synthetic '_user' persona for post-mortem injections
    isn't a participant — leaderboard must skip it."""
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett", "_user"],
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    assert all(r["persona_id"] != "_user" for r in out)


@pytest.mark.asyncio
async def test_window_filter(db_session: AsyncSession, owner: User):
    """A discussion 100 days old shouldn't appear in days=30."""
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett"], days_old=100,
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["lynch"], days_old=5,
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id, days=30,
    )
    by_id = {r["persona_id"]: r for r in out}
    assert "lynch" in by_id
    assert "buffett" not in by_id


@pytest.mark.asyncio
async def test_market_filter(db_session: AsyncSession, owner: User):
    await _make_discussion(
        db_session, owner.id,
        persona_ids=["buffett"], market="US",
    )
    await _make_discussion(
        db_session, owner.id,
        persona_ids=["lynch"], market="TW",
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id, market="TW",
    )
    by_id = {r["persona_id"]: r for r in out}
    assert "lynch" in by_id
    assert "buffett" not in by_id


# ── strategy-scoped weight stats ─────────────────────────────────


@pytest.mark.asyncio
async def test_weights_only_when_strategy_scoped(
    db_session: AsyncSession, owner: User,
):
    """Without strategy_id, average_weight + weight_trend stay None."""
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett"],
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    assert out[0]["average_weight"] is None
    assert out[0]["weight_trend_30d"] is None


@pytest.mark.asyncio
async def test_weight_trend_recent_minus_baseline(
    db_session: AsyncSession, owner: User,
):
    """8 weight versions newest-first: recent 3 = avg(1.5, 1.4, 1.6)
    = 1.5; baseline 5 = avg(0.5, 0.6, 0.55, 0.6, 0.5) = 0.55. Trend
    = 0.95."""
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner.id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=["buffett"],
    )
    db_session.add(tmpl)
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner.id, strategy_id=tmpl.id,
        market="TW", topic="x", rules="",
        persona_ids=["buffett"],
        anchor_date=date.today(),
        trading_days_count=1, rounds_per_discussion=1,
        concurrency=1, status="completed",
    )
    db_session.add(sweep)
    await db_session.flush()
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett"],
        sweep_id=sweep.id,
    )
    # Versions newest → oldest by fit_at
    weights = [1.5, 1.4, 1.6, 0.5, 0.6, 0.55, 0.6, 0.5]
    now = datetime.now(UTC)
    for i, w in enumerate(weights):
        db_session.add(StrategyVersion(
            id=uuid4(), strategy_id=tmpl.id,
            version_number=len(weights) - i,
            artifact_kind="weights", payload={"buffett": w},
            fit_at=now - timedelta(hours=i),
            status="active" if i == 0 else "superseded",
        ))
    await db_session.commit()

    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id, strategy_id=tmpl.id,
    )
    by_id = {r["persona_id"]: r for r in out}
    assert by_id["buffett"]["average_weight"] == pytest.approx(0.90625, abs=1e-3)
    assert by_id["buffett"]["weight_trend_30d"] == pytest.approx(0.95, abs=1e-3)


# ── status counts ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_counts_across_strategies(
    db_session: AsyncSession, owner: User,
):
    """A persona frozen on 2 strategies + shadow on 1 should see
    those counts in the leaderboard row."""
    await _make_discussion(
        db_session, owner.id, persona_ids=["buffett"],
    )
    for status, n in [("frozen", 2), ("shadow", 1)]:
        for _ in range(n):
            db_session.add(DiscussionStrategyTemplate(
                id=uuid4(), owner_id=owner.id,
                name="t", topic="x", rules="", market="TW",
                persona_ids=["buffett"],
                persona_status={"buffett": status},
            ))
    await db_session.commit()
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    by_id = {r["persona_id"]: r for r in out}
    assert by_id["buffett"]["frozen_in_strategies"] == 2
    assert by_id["buffett"]["shadow_in_strategies"] == 1


@pytest.mark.asyncio
async def test_results_sorted_by_win_rate_then_participation(
    db_session: AsyncSession, owner: User,
):
    """Persona A: 3 wins / 5 participations = 0.6
       Persona B: 2 wins / 2 participations = 1.0 ← sorts first
       Persona C: 1 win / 5 participations = 0.2"""
    await _make_discussion(
        db_session, owner.id, persona_ids=["A", "C"], verdict="win",
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["A", "C"], verdict="win",
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["A", "C"], verdict="win",
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["A", "C"], verdict="loss",
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["A", "C"], verdict="loss",
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["B"], verdict="win",
    )
    await _make_discussion(
        db_session, owner.id, persona_ids=["B"], verdict="win",
    )
    out = await svc.compute_persona_performance(
        db_session, owner_id=owner.id,
    )
    # B (rate 1.0) → A (0.6) → C (0.4 — wait: same as A; 3/5=0.6)
    # Both A and C have 3 wins out of 5 participations because they
    # always discussed together. B has 2/2=1.0 → sorts first.
    persona_ids = [r["persona_id"] for r in out]
    assert persona_ids[0] == "B"
