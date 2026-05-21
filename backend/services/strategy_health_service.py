"""Strategy health metrics computation + persistence (PR-4b).

Daily snapshot of every active strategy's rolling-30 KPIs.
Consumed by:

  - The cron `tasks.monitor_strategy_health` (one snapshot per
    strategy per day at 02:00 UTC).
  - The `GET /api/discussion/strategies/{id}/health` endpoint
    which reads back N days for the UI sparkline.
  - The admin alert pipeline — any non-empty `status_flags` array
    fires a notification via `notification_service`.

Decision: status_flags is computed from the CURRENT row vs the
HISTORICAL series in the same table, NOT from raw discussions.
This keeps the cron self-contained (idempotent re-run on yesterday
gives the same result) and decouples flag tuning from the brier
math.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric

log = logging.getLogger(__name__)


# Window + threshold tuning. All conservative — bumping the
# `BRIER_DRIFT_RATIO` down (e.g. 1.15) catches more drift earlier;
# loosening it (e.g. 1.30) reduces noise. The current 1.20 mirrors
# `strategy_maturity_service.DRIFT_RATIO_THRESHOLD` so a strategy
# flagged drifting in the maturity classifier ALSO carries a
# `brier_drift` flag here.
WINDOW_DAYS = 30
MIN_SAMPLES_FOR_METRIC = 5
BRIER_DRIFT_RATIO = 1.20
LOW_SAMPLE_THRESHOLD = 5
LESSON_HIT_COLLAPSE_THRESHOLD = 0.30


async def _gather_recent_briers(
    db: AsyncSession, strategy_id: UUID, *, snapshot: date,
) -> tuple[list[float], list[float | None]]:
    """Returns (raw_briers, calibrated_briers) over the rolling
    `WINDOW_DAYS` ending on `snapshot` (inclusive).

    `calibrated_briers` may have None entries for discussions
    pre-PR-C2 (no calibrated_confidence set); the caller filters.
    """
    sweep_ids = list((await db.scalars(
        select(BacktestSweep.id).where(
            BacktestSweep.strategy_id == strategy_id,
        )
    )).all())
    if not sweep_ids:
        return ([], [])
    window_start = datetime.combine(
        snapshot - timedelta(days=WINDOW_DAYS), datetime.min.time(), UTC,
    )
    window_end = datetime.combine(
        snapshot + timedelta(days=1), datetime.min.time(), UTC,
    )
    rows = list((await db.scalars(
        select(
            Discussion,
        ).where(
            Discussion.sweep_id.in_(sweep_ids),
            Discussion.brier_score.is_not(None),
            Discussion.created_at >= window_start,
            Discussion.created_at < window_end,
        )
        .order_by(Discussion.created_at.asc())
    )).all())
    raw = [float(r.brier_score) for r in rows]
    calibrated = [
        float(r.calibrated_brier_score)
        if r.calibrated_brier_score is not None else None
        for r in rows
    ]
    return (raw, calibrated)


async def _gather_window_hit_rate(
    db: AsyncSession, strategy_id: UUID, *, snapshot: date,
) -> tuple[float | None, int]:
    """win_count / total_count over the rolling window. Returns
    `(rate, sample_count)`; rate is None when nothing graded."""
    sweep_ids = list((await db.scalars(
        select(BacktestSweep.id).where(
            BacktestSweep.strategy_id == strategy_id,
        )
    )).all())
    if not sweep_ids:
        return (None, 0)
    window_start = datetime.combine(
        snapshot - timedelta(days=WINDOW_DAYS), datetime.min.time(), UTC,
    )
    window_end = datetime.combine(
        snapshot + timedelta(days=1), datetime.min.time(), UTC,
    )
    verdicts = list((await db.scalars(
        select(Discussion.verdict).where(
            Discussion.sweep_id.in_(sweep_ids),
            Discussion.verdict.is_not(None),
            Discussion.created_at >= window_start,
            Discussion.created_at < window_end,
        )
    )).all())
    if not verdicts:
        return (None, 0)
    from services.outcome_classifier import is_winning_verdict
    total = len(verdicts)
    wins = sum(1 for v in verdicts if is_winning_verdict(v))
    return (wins / total, total)


async def _gather_owner_lesson_hit_rate(
    db: AsyncSession, owner_id: UUID, market: str,
) -> float | None:
    """Lessons accumulate market-wide per owner; not strategy-
    scoped (lessons are reusable across strategies of the same
    market). Returns hit_count / usage_count summed across all
    non-archived lessons in this owner+market, or None when no
    lesson has been used yet."""
    rows = list((await db.scalars(
        select(DiscussionLesson).where(
            DiscussionLesson.owner_user_id == owner_id,
            DiscussionLesson.market == market,
        )
    )).all())
    used = [r for r in rows if (r.usage_count or 0) > 0]
    if not used:
        return None
    total_usage = sum(r.usage_count or 0 for r in used)
    total_hits = sum(r.hit_count or 0 for r in used)
    if total_usage == 0:
        return None
    return total_hits / total_usage


def _compute_status_flags(
    *,
    sample_count: int,
    brier_30d: float | None,
    historical_brier_mean: float | None,
    lesson_hit_rate: float | None,
) -> list[str]:
    """Tag the row with diagnostic flags. None / partial-data paths
    return an empty list — `low_sample` is a flag IN ITS OWN RIGHT
    so the operator sees "we don't have enough to compute" rather
    than a false-negative clean snapshot.
    """
    flags: list[str] = []
    if sample_count < LOW_SAMPLE_THRESHOLD:
        flags.append("low_sample")
        # Don't compute drift on insufficient sample — keeps the
        # signal meaningful.
        return flags
    if (
        brier_30d is not None
        and historical_brier_mean is not None
        and historical_brier_mean > 0
        and brier_30d / historical_brier_mean >= BRIER_DRIFT_RATIO
    ):
        flags.append("brier_drift")
    if (
        lesson_hit_rate is not None
        and lesson_hit_rate < LESSON_HIT_COLLAPSE_THRESHOLD
    ):
        flags.append("lesson_hit_collapse")
    return flags


async def compute_snapshot(
    db: AsyncSession,
    strategy_id: UUID,
    *,
    snapshot_date: date | None = None,
) -> dict[str, Any]:
    """Compute (but don't persist) one day's metrics for a strategy.

    Returns the dict the caller can persist via `record_snapshot` or
    pass to the admin alert pipeline.
    """
    snap = snapshot_date or datetime.now(UTC).date()

    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
        )
    )
    if tmpl is None:
        return {
            "strategy_id": strategy_id,
            "snapshot_date": snap,
            "brier_30d": None,
            "calibrated_brier_30d": None,
            "hit_rate_30d": None,
            "sample_count_30d": 0,
            "lesson_hit_rate_30d": None,
            "maturity_tier_at_snapshot": None,
            "status_flags": ["strategy_missing"],
        }

    raw_briers, calibrated_briers = await _gather_recent_briers(
        db, strategy_id, snapshot=snap,
    )
    sample_count = len(raw_briers)
    brier_30d = (
        sum(raw_briers) / len(raw_briers)
        if raw_briers else None
    )
    cal_present = [c for c in calibrated_briers if c is not None]
    calibrated_brier_30d = (
        sum(cal_present) / len(cal_present)
        if cal_present else None
    )
    hit_rate_30d, _ = await _gather_window_hit_rate(
        db, strategy_id, snapshot=snap,
    )
    lesson_hit = await _gather_owner_lesson_hit_rate(
        db, owner_id=tmpl.owner_id, market=tmpl.market,
    )

    # Historical baseline = mean brier across all OLDER snapshots
    # in the same table (excludes today). Falling back to the
    # rolling window if no history yet — lets the first few days
    # build their own baseline organically.
    history = list((await db.scalars(
        select(StrategyHealthMetric.brier_30d).where(
            StrategyHealthMetric.strategy_id == strategy_id,
            StrategyHealthMetric.snapshot_date < snap,
            StrategyHealthMetric.brier_30d.is_not(None),
        )
    )).all())
    historical_mean = (
        sum(float(h) for h in history) / len(history)
        if history else None
    )

    flags = _compute_status_flags(
        sample_count=sample_count,
        brier_30d=brier_30d,
        historical_brier_mean=historical_mean,
        lesson_hit_rate=lesson_hit,
    )

    return {
        "strategy_id": strategy_id,
        "snapshot_date": snap,
        "brier_30d": brier_30d,
        "calibrated_brier_30d": calibrated_brier_30d,
        "hit_rate_30d": hit_rate_30d,
        "sample_count_30d": sample_count,
        "lesson_hit_rate_30d": lesson_hit,
        "maturity_tier_at_snapshot": tmpl.maturity_tier,
        "status_flags": flags,
    }


async def record_snapshot(
    db: AsyncSession,
    strategy_id: UUID,
    *,
    snapshot_date: date | None = None,
) -> StrategyHealthMetric:
    """Compute + upsert one day's metrics. Idempotent: re-running
    for the same `(strategy_id, snapshot_date)` overwrites the row
    so a re-tick of the cron after a hiccup recomputes cleanly.
    """
    payload = await compute_snapshot(
        db, strategy_id, snapshot_date=snapshot_date,
    )
    existing = await db.scalar(
        select(StrategyHealthMetric).where(
            StrategyHealthMetric.strategy_id == strategy_id,
            StrategyHealthMetric.snapshot_date == payload["snapshot_date"],
        )
    )
    if existing is None:
        row = StrategyHealthMetric(
            strategy_id=strategy_id,
            snapshot_date=payload["snapshot_date"],
            brier_30d=payload["brier_30d"],
            calibrated_brier_30d=payload["calibrated_brier_30d"],
            hit_rate_30d=payload["hit_rate_30d"],
            sample_count_30d=payload["sample_count_30d"],
            lesson_hit_rate_30d=payload["lesson_hit_rate_30d"],
            maturity_tier_at_snapshot=payload["maturity_tier_at_snapshot"],
            status_flags=payload["status_flags"],
        )
        db.add(row)
    else:
        existing.brier_30d = payload["brier_30d"]
        existing.calibrated_brier_30d = payload["calibrated_brier_30d"]
        existing.hit_rate_30d = payload["hit_rate_30d"]
        existing.sample_count_30d = payload["sample_count_30d"]
        existing.lesson_hit_rate_30d = payload["lesson_hit_rate_30d"]
        existing.maturity_tier_at_snapshot = payload["maturity_tier_at_snapshot"]
        existing.status_flags = payload["status_flags"]
        existing.computed_at = datetime.now(UTC)
        row = existing
    await db.commit()
    await db.refresh(row)
    return row


async def list_recent_snapshots(
    db: AsyncSession,
    strategy_id: UUID,
    *,
    days: int = 30,
) -> list[StrategyHealthMetric]:
    """Newest-first read for the UI sparkline."""
    cutoff = datetime.now(UTC).date() - timedelta(days=max(int(days), 1))
    rows = list((await db.scalars(
        select(StrategyHealthMetric)
        .where(
            StrategyHealthMetric.strategy_id == strategy_id,
            StrategyHealthMetric.snapshot_date >= cutoff,
        )
        .order_by(StrategyHealthMetric.snapshot_date.desc())
    )).all())
    return rows


def snapshot_to_dict(row: StrategyHealthMetric) -> dict[str, Any]:
    return {
        "strategy_id": str(row.strategy_id),
        "snapshot_date": (
            row.snapshot_date.isoformat() if row.snapshot_date else None
        ),
        "brier_30d": row.brier_30d,
        "calibrated_brier_30d": row.calibrated_brier_30d,
        "hit_rate_30d": row.hit_rate_30d,
        "sample_count_30d": row.sample_count_30d,
        "lesson_hit_rate_30d": row.lesson_hit_rate_30d,
        "maturity_tier_at_snapshot": row.maturity_tier_at_snapshot,
        "status_flags": list(row.status_flags or []),
        "computed_at": (
            row.computed_at.isoformat() if row.computed_at else None
        ),
    }


__all__ = [
    "WINDOW_DAYS",
    "MIN_SAMPLES_FOR_METRIC",
    "BRIER_DRIFT_RATIO",
    "LOW_SAMPLE_THRESHOLD",
    "LESSON_HIT_COLLAPSE_THRESHOLD",
    "compute_snapshot",
    "record_snapshot",
    "list_recent_snapshots",
    "snapshot_to_dict",
]
