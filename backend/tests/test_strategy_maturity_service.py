"""PR-4a: tests for the strategy maturity classifier.

Drives the five-tier rule across cold_start / learning / mature /
drifting / stale by constructing the right combination of
sweeps + brier history + curve presence on a test strategy.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import strategy_maturity_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"mat-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *,
    curve: list | None = None,
    sample_count: int | None = None,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=["market_analyst"],
        calibration_curve=curve,
        calibration_sample_count=sample_count,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _add_sweep(
    db: AsyncSession, owner_id: uuid.UUID, strategy_id: uuid.UUID,
    *,
    completed_at: datetime,
    fold_kind: str = "production",
    status: str = "completed",
) -> BacktestSweep:
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner_id, strategy_id=strategy_id,
        market="TW", topic="x", rules="",
        persona_ids=["market_analyst"],
        anchor_date=datetime.now(UTC).date(),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, status=status,
        fold_kind=fold_kind, completed_at=completed_at,
    )
    db.add(sweep)
    await db.commit()
    await db.refresh(sweep)
    return sweep


async def _add_discussions_with_brier(
    db: AsyncSession, owner_id: uuid.UUID, sweep_id: uuid.UUID,
    *,
    briers: list[float],
    days_old_start: int = 100,
) -> None:
    """Create N discussions with the given brier scores. Set
    `created_at` so the chronological order is older→newer through
    the list (oldest at index 0)."""
    base = datetime.now(UTC) - timedelta(days=days_old_start)
    for i, b in enumerate(briers):
        d = Discussion(
            id=uuid4(), owner_id=owner_id, sweep_id=sweep_id,
            topic="x", rules="", market="TW",
            persona_ids=["market_analyst"],
            status="done", current_round=1,
            brier_score=b,
            created_at=base + timedelta(hours=i),
            updated_at=base + timedelta(hours=i),
        )
        db.add(d)
    await db.commit()


# ── tier rules ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_start_when_no_sweep(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    tier, signals = await svc.compute_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "cold_start"
    assert signals["production_sweep_count"] == 0


@pytest.mark.asyncio
async def test_learning_when_sweep_exists_but_no_curve(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    await _add_sweep(
        db_session, owner.id, tmpl.id,
        completed_at=datetime.now(UTC) - timedelta(days=1),
    )
    tier, signals = await svc.compute_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "learning"
    assert signals["production_sweep_count"] == 1
    assert signals["has_calibration_curve"] is False


@pytest.mark.asyncio
async def test_learning_when_curve_but_too_few_samples(
    db_session: AsyncSession, owner: User,
):
    """30 sample threshold — 29 falls back to learning."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        curve=[{"raw": 0.5, "calibrated": 0.4}],
        sample_count=29,
    )
    for _ in range(3):
        await _add_sweep(
            db_session, owner.id, tmpl.id,
            completed_at=datetime.now(UTC) - timedelta(days=1),
        )
    tier, _ = await svc.compute_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "learning"


@pytest.mark.asyncio
async def test_mature_when_all_thresholds_met(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(
        db_session, owner.id,
        curve=[{"raw": 0.5, "calibrated": 0.4}],
        sample_count=50,
    )
    for _ in range(3):
        await _add_sweep(
            db_session, owner.id, tmpl.id,
            completed_at=datetime.now(UTC) - timedelta(days=1),
        )
    tier, _ = await svc.compute_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "mature"


@pytest.mark.asyncio
async def test_drifting_when_recent_brier_degrades(
    db_session: AsyncSession, owner: User,
):
    """Recent 30-sample brier 1.5× the baseline → drifting (over
    the 1.20 threshold). Even if the strategy has otherwise mature
    inputs, the drift signal wins."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        curve=[{"raw": 0.5, "calibrated": 0.4}],
        sample_count=50,
    )
    for _ in range(3):
        await _add_sweep(
            db_session, owner.id, tmpl.id,
            completed_at=datetime.now(UTC) - timedelta(days=1),
        )
    from sqlalchemy import select as _sa_select
    sweep_id = (await db_session.scalars(
        _sa_select(BacktestSweep.id).where(
            BacktestSweep.strategy_id == tmpl.id,
        ).limit(1),
    )).first()
    # Old: brier 0.20 × 10 samples baseline. Recent: brier 0.30 × 30 samples → ratio 1.5
    await _add_discussions_with_brier(
        db_session, owner.id, sweep_id,
        briers=[0.20] * 10 + [0.30] * 30,
    )
    tier, signals = await svc.compute_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "drifting"
    assert signals["brier_ratio"] == 1.5


@pytest.mark.asyncio
async def test_stale_when_no_recent_sweep(
    db_session: AsyncSession, owner: User,
):
    """A previously-mature strategy that hasn't run in >30 days
    flips to stale regardless of sample count."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        curve=[{"raw": 0.5, "calibrated": 0.4}],
        sample_count=50,
    )
    for _ in range(3):
        await _add_sweep(
            db_session, owner.id, tmpl.id,
            completed_at=datetime.now(UTC) - timedelta(days=45),
        )
    tier, _ = await svc.compute_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "stale"


# ── update_maturity_tier persists ───────────────────────────────


@pytest.mark.asyncio
async def test_update_persists_tier_and_timestamp(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    await _add_sweep(
        db_session, owner.id, tmpl.id,
        completed_at=datetime.now(UTC) - timedelta(days=1),
    )
    tier, _ = await svc.update_maturity_tier(
        db_session, strategy_id=tmpl.id,
    )
    assert tier == "learning"

    await db_session.refresh(tmpl)
    assert tmpl.maturity_tier == "learning"
    assert tmpl.maturity_computed_at is not None


@pytest.mark.asyncio
async def test_unknown_strategy_returns_cold_start(
    db_session: AsyncSession,
):
    """Defensive: bogus strategy_id → cold_start, not raise."""
    tier, signals = await svc.compute_maturity_tier(
        db_session, strategy_id=uuid4(),
    )
    assert tier == "cold_start"
    assert signals == {}
