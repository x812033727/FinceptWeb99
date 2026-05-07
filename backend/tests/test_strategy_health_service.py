"""PR-4b: tests for `strategy_health_service` — daily snapshot
computation + persistence + status flag derivation.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric
from models.user import User, UserRole
from services import strategy_health_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"health-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *, market: str = "TW", maturity_tier: str = "cold_start",
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market=market,
        persona_ids=["market_analyst"],
        maturity_tier=maturity_tier,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _add_sweep_with_briers(
    db: AsyncSession, owner_id: uuid.UUID, strategy_id: uuid.UUID,
    *,
    briers: list[float],
    calibrated: list[float | None] | None = None,
    verdicts: list[str] | None = None,
    days_old_start: int = 0,
) -> uuid.UUID:
    """Create a sweep + N discussions, with the discussions
    chronologically spaced through the rolling window. Each
    discussion gets the next brier value (and optional calibrated /
    verdict) so the snapshot computation has data to crunch."""
    sweep = BacktestSweep(
        id=uuid4(), owner_id=owner_id, strategy_id=strategy_id,
        market="TW", topic="x", rules="",
        persona_ids=["market_analyst"],
        anchor_date=datetime.now(UTC).date(),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, status="completed",
    )
    db.add(sweep)
    await db.flush()

    base = datetime.now(UTC) - timedelta(days=days_old_start)
    for i, b in enumerate(briers):
        d = Discussion(
            id=uuid4(), owner_id=owner_id, sweep_id=sweep.id,
            topic="x", rules="", market="TW",
            persona_ids=["market_analyst"],
            status="concluded", current_round=1,
            brier_score=b,
            calibrated_brier_score=(
                calibrated[i] if calibrated and i < len(calibrated) else None
            ),
            verdict=(
                verdicts[i] if verdicts and i < len(verdicts) else None
            ),
            created_at=base + timedelta(hours=i),
            updated_at=base + timedelta(hours=i),
        )
        db.add(d)
    await db.commit()
    return sweep.id


# ── compute_snapshot ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_zero_data_returns_low_sample_flag(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    out = await svc.compute_snapshot(db_session, strategy_id=tmpl.id)
    assert out["sample_count_30d"] == 0
    assert out["brier_30d"] is None
    assert "low_sample" in out["status_flags"]


@pytest.mark.asyncio
async def test_snapshot_computes_brier_and_hit_rate(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.20] * 10,
        verdicts=["win"] * 6 + ["loss"] * 4,
    )
    out = await svc.compute_snapshot(db_session, strategy_id=tmpl.id)
    assert out["sample_count_30d"] == 10
    assert out["brier_30d"] == pytest.approx(0.20)
    assert out["hit_rate_30d"] == pytest.approx(0.6)
    # No history yet → no drift to flag → no flags above sample
    assert "brier_drift" not in out["status_flags"]


@pytest.mark.asyncio
async def test_snapshot_calibrated_brier_only_when_present(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.20] * 8,
        calibrated=[0.15, 0.18, None, None, None, None, None, None],
    )
    out = await svc.compute_snapshot(db_session, strategy_id=tmpl.id)
    # Two non-NULL calibrated → mean over those two
    assert out["calibrated_brier_30d"] == pytest.approx(0.165)


# ── status flag derivation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_brier_drift_flag_against_history(
    db_session: AsyncSession, owner: User,
):
    """Seed an older snapshot row at brier=0.20, then compute a
    new one with brier 1.5× → should fire `brier_drift`."""
    tmpl = await _make_strategy(db_session, owner.id)
    # Historical row
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=date.today() - timedelta(days=3),
        brier_30d=0.20,
        sample_count_30d=10,
        status_flags=[],
    ))
    await db_session.commit()
    # Today's data: brier 0.30
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.30] * 10,
    )
    out = await svc.compute_snapshot(db_session, strategy_id=tmpl.id)
    assert "brier_drift" in out["status_flags"]


@pytest.mark.asyncio
async def test_low_sample_short_circuits_drift_check(
    db_session: AsyncSession, owner: User,
):
    """Low sample count → `low_sample` flag fires alone; even if
    the brier looks degraded, we don't fire `brier_drift` on
    insufficient data (would be a false positive)."""
    tmpl = await _make_strategy(db_session, owner.id)
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=date.today() - timedelta(days=3),
        brier_30d=0.20,
        sample_count_30d=10,
        status_flags=[],
    ))
    await db_session.commit()
    # Only 3 brier samples → low_sample
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.50, 0.50, 0.50],
    )
    out = await svc.compute_snapshot(db_session, strategy_id=tmpl.id)
    assert "low_sample" in out["status_flags"]
    assert "brier_drift" not in out["status_flags"]


# ── record_snapshot persists + idempotent ────────────────────────


@pytest.mark.asyncio
async def test_record_snapshot_writes_new_row(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.20] * 10,
    )
    row = await svc.record_snapshot(db_session, strategy_id=tmpl.id)
    assert row.snapshot_date == date.today()
    assert row.brier_30d == pytest.approx(0.20)
    assert row.sample_count_30d == 10


@pytest.mark.asyncio
async def test_record_snapshot_idempotent_upsert(
    db_session: AsyncSession, owner: User,
):
    """Re-running for the same `(strategy, date)` overwrites in
    place — a hiccup-recovery cron run shouldn't insert duplicates."""
    tmpl = await _make_strategy(db_session, owner.id)
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.20] * 5,
    )
    r1 = await svc.record_snapshot(db_session, strategy_id=tmpl.id)
    # Add more discussions; recompute
    await _add_sweep_with_briers(
        db_session, owner.id, tmpl.id,
        briers=[0.30] * 10,
    )
    r2 = await svc.record_snapshot(db_session, strategy_id=tmpl.id)
    # Same primary key → same row, updated values
    assert r1.snapshot_date == r2.snapshot_date
    assert r2.sample_count_30d == 15
    # mean of 5×0.20 + 10×0.30 = 0.2667
    assert r2.brier_30d == pytest.approx(0.2667, abs=1e-3)


@pytest.mark.asyncio
async def test_list_recent_returns_newest_first(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    today = date.today()
    for offset in (5, 3, 1):
        db_session.add(StrategyHealthMetric(
            strategy_id=tmpl.id,
            snapshot_date=today - timedelta(days=offset),
            brier_30d=0.20,
            sample_count_30d=10,
            status_flags=[],
        ))
    await db_session.commit()
    rows = await svc.list_recent_snapshots(
        db_session, strategy_id=tmpl.id, days=10,
    )
    assert len(rows) == 3
    assert rows[0].snapshot_date == today - timedelta(days=1)
    assert rows[2].snapshot_date == today - timedelta(days=5)


@pytest.mark.asyncio
async def test_list_recent_filters_by_window(
    db_session: AsyncSession, owner: User,
):
    """An ancient row outside the days window doesn't return."""
    tmpl = await _make_strategy(db_session, owner.id)
    today = date.today()
    db_session.add(StrategyHealthMetric(
        strategy_id=tmpl.id,
        snapshot_date=today - timedelta(days=100),
        brier_30d=0.20, sample_count_30d=10, status_flags=[],
    ))
    await db_session.commit()
    rows = await svc.list_recent_snapshots(
        db_session, strategy_id=tmpl.id, days=30,
    )
    assert rows == []


# ── snapshot_to_dict ─────────────────────────────────────────────


def test_snapshot_to_dict_handles_minimal_row():
    row = StrategyHealthMetric(
        strategy_id=uuid4(),
        snapshot_date=date(2026, 5, 7),
        brier_30d=None,
        calibrated_brier_30d=None,
        hit_rate_30d=None,
        sample_count_30d=0,
        lesson_hit_rate_30d=None,
        maturity_tier_at_snapshot=None,
        status_flags=["low_sample"],
        computed_at=datetime(2026, 5, 7, 2, 0, tzinfo=UTC),
    )
    out = svc.snapshot_to_dict(row)
    assert out["snapshot_date"] == "2026-05-07"
    assert out["status_flags"] == ["low_sample"]
    assert out["computed_at"] == "2026-05-07T02:00:00+00:00"
