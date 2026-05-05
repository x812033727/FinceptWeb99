"""Tests for `services.persona_weight_learner` (PR-C)."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import persona_weight_learner as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        email=f"learn-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.fixture
async def strategy(
    db_session: AsyncSession, owner: User,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        owner_id=owner.id,
        name="learner-test", topic="t", rules="r",
        market="TW", persona_ids=["bull", "bear", "neutral"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=True,
    )
    db_session.add(tmpl)
    await db_session.commit()
    await db_session.refresh(tmpl)
    return tmpl


def _make_sweep(owner_id, strategy_id, persona_ids):
    return BacktestSweep(
        owner_id=owner_id, strategy_id=strategy_id,
        topic="t", rules="r", market="TW",
        persona_ids=persona_ids,
        anchor_date=date(2026, 1, 5),
        trading_days_count=3,
        rounds_per_discussion=1, concurrency=1,
        auto_post_mortem=True,
    )


def _make_disc(owner_id, sweep_id, persona_ids, verdict, as_of):
    return Discussion(
        owner_id=owner_id, sweep_id=sweep_id,
        topic="t", rules="r", persona_ids=persona_ids,
        market="TW", status="done", current_round=1,
        as_of_date=as_of, verdict=verdict,
    )


# ── compute_weights_from_aggregate (pure) ──────────────────────


def test_compute_weights_centers_on_one():
    weights = svc.compute_weights_from_aggregate([
        {"persona_id": "a", "hit_rate": 0.5, "discussions_count": 10},
        {"persona_id": "b", "hit_rate": 0.5, "discussions_count": 10},
    ])
    # Equal hit-rate -> weight 1.0 each (within rounding).
    assert weights == {"a": 1.0, "b": 1.0}


def test_compute_weights_high_persona_above_one():
    weights = svc.compute_weights_from_aggregate([
        {"persona_id": "winner", "hit_rate": 0.8, "discussions_count": 10},
        {"persona_id": "loser", "hit_rate": 0.2, "discussions_count": 10},
    ])
    assert weights["winner"] > weights["loser"]
    assert weights["winner"] > 1.0
    assert weights["loser"] < 1.0


def test_compute_weights_clamped_to_floor_and_ceil():
    weights = svc.compute_weights_from_aggregate([
        {"persona_id": "perfect", "hit_rate": 1.0, "discussions_count": 10},
        {"persona_id": "zero", "hit_rate": 0.0, "discussions_count": 10},
    ])
    assert weights["perfect"] <= svc.WEIGHT_CEIL
    assert weights["zero"] >= svc.WEIGHT_FLOOR


# ── format_weights_for_synthesizer ─────────────────────────────


def test_format_empty_weights_returns_empty_string():
    assert svc.format_weights_for_synthesizer({}) == ""


def test_format_orders_high_to_low():
    txt = svc.format_weights_for_synthesizer(
        {"low": 0.7, "high": 1.3, "mid": 1.0}
    )
    # High-to-low ordering — `high` line appears before `low`.
    assert txt.find("`high`") < txt.find("`mid`") < txt.find("`low`")
    assert "權重" in txt


# ── learn_weights_for_strategy (DB-backed) ─────────────────────


@pytest.mark.asyncio
async def test_learn_skipped_when_below_min_samples(
    db_session: AsyncSession, owner: User,
    strategy: DiscussionStrategyTemplate,
):
    sweep = _make_sweep(owner.id, strategy.id, strategy.persona_ids)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # Single discussion -> below MIN_SAMPLES=3.
    db_session.add(_make_disc(
        owner.id, sweep.id, strategy.persona_ids, "win", date(2026, 1, 5),
    ))
    await db_session.commit()

    result = await svc.learn_weights_for_strategy(
        db_session, owner_id=owner.id, strategy_id=strategy.id,
    )
    assert result["updated"] is False
    assert "MIN_SAMPLES" in result["reason"]
    # weights stayed empty / unchanged
    assert result["weights"] == {}


@pytest.mark.asyncio
async def test_learn_writes_weights_when_eligible(
    db_session: AsyncSession, owner: User,
    strategy: DiscussionStrategyTemplate,
):
    sweep = _make_sweep(owner.id, strategy.id, strategy.persona_ids)
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    # 4 discussions all using full roster: 3 wins + 1 loss.
    # Every persona's hit_rate = 3/4. Equal -> weights all 1.0.
    for i, verdict in enumerate(["win", "win", "win", "loss"]):
        db_session.add(_make_disc(
            owner.id, sweep.id, strategy.persona_ids, verdict,
            date(2026, 1, 5 + i),
        ))
    await db_session.commit()

    result = await svc.learn_weights_for_strategy(
        db_session, owner_id=owner.id, strategy_id=strategy.id,
    )
    assert result["updated"] is True
    assert result["reason"] is None
    assert set(result["weights"].keys()) == {"bull", "bear", "neutral"}
    for w in result["weights"].values():
        assert w == pytest.approx(1.0)

    # Persisted on the row.
    await db_session.refresh(strategy)
    assert strategy.persona_weights == result["weights"]
    assert strategy.weights_updated_at is not None


@pytest.mark.asyncio
async def test_learn_raises_when_template_missing(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError):
        await svc.learn_weights_for_strategy(
            db_session, owner_id=owner.id, strategy_id=uuid.uuid4(),
        )
