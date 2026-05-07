"""Strategy lifecycle maturity classification (PR-4a).

Five-tier classification based on how much production-fit work the
strategy has accumulated and whether its recent calibration is
holding up vs the historical baseline:

  - cold_start  : no production sweep has finished yet
  - learning    : sweeps started but <30 calibration samples OR <3
                  production sweeps OR no calibration curve
  - mature      : ≥30 samples + ≥3 sweeps + recent brier within
                  ±15% of historical baseline
  - drifting    : recent 30-sample brier ≥20% above baseline
  - stale       : no production sweep in the last STALE_AFTER_DAYS

Computation is cheap (a few aggregate queries) and pure read.
Called from `backtest_sweep_service` Phase 3 finalisation and from
the upcoming PR-4b health cron — neither path blocks the sweep
worker on classification failure.

Returns both the tier and a `signals` dict telling the UI _why_
the strategy landed in this bucket so the operator doesn't have
to dig.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion_strategy_template import DiscussionStrategyTemplate

log = logging.getLogger(__name__)


# Thresholds — picked conservative by design. A `learning` strategy
# graduates to `mature` only after enough sweeps that the persona
# weights and calibration curve have stopped shifting; bumping these
# up is safe (more conservative graduation) but down risks
# under-baked strategies showing up as "mature" in the UI.
MIN_SAMPLES_FOR_MATURE = 30        # matches confidence_calibrator.MIN_SAMPLES_FOR_FIT
MIN_PRODUCTION_SWEEPS_FOR_MATURE = 3
DRIFT_RATIO_THRESHOLD = 1.20       # recent / baseline brier
STALE_AFTER_DAYS = 30


async def _count_production_sweeps(
    db: AsyncSession, strategy_id: UUID,
) -> tuple[int, datetime | None]:
    """Returns (count, latest_completed_at) over completed
    production-fold sweeps for this strategy."""
    rows = list((await db.scalars(
        select(BacktestSweep.completed_at)
        .where(
            BacktestSweep.strategy_id == strategy_id,
            BacktestSweep.status == "completed",
            BacktestSweep.fold_kind == "production",
            BacktestSweep.completed_at.is_not(None),
        )
        .order_by(BacktestSweep.completed_at.desc())
    )).all())
    if not rows:
        return (0, None)
    return (len(rows), rows[0])


async def _split_brier_recent_baseline(
    db: AsyncSession, strategy_id: UUID,
    *,
    recent_n: int = 30,
    minimum_baseline: int = 5,
) -> tuple[float | None, float | None]:
    """Pull every concluded discussion's `brier_score` for this
    strategy in chronological order and split into the most-recent
    `recent_n` vs everything older. Returns the means of each half,
    or (None, None) when there isn't enough data on either side
    to draw a comparison."""
    sweep_ids = list((await db.scalars(
        select(BacktestSweep.id).where(
            BacktestSweep.strategy_id == strategy_id,
        )
    )).all())
    if not sweep_ids:
        return (None, None)
    rows = list((await db.scalars(
        select(Discussion.brier_score)
        .where(
            Discussion.sweep_id.in_(sweep_ids),
            Discussion.brier_score.is_not(None),
        )
        .order_by(Discussion.created_at.asc())
    )).all())
    if len(rows) < recent_n + minimum_baseline:
        return (None, None)
    recent = [float(r) for r in rows[-recent_n:]]
    baseline = [float(r) for r in rows[:-recent_n]]
    if not recent or not baseline:
        return (None, None)
    return (sum(recent) / len(recent), sum(baseline) / len(baseline))


async def compute_maturity_tier(
    db: AsyncSession, strategy_id: UUID,
) -> tuple[str, dict[str, Any]]:
    """Classify the strategy and return (tier, signals_dict).

    `signals` carries every input the rule used so the UI can render
    a tooltip "why this tier" without a second round-trip:

      {
        "production_sweep_count": int,
        "latest_sweep_completed_at": str | None,
        "calibration_sample_count": int | None,
        "has_calibration_curve": bool,
        "recent_brier": float | None,
        "baseline_brier": float | None,
        "brier_ratio": float | None,
      }
    """
    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
        )
    )
    if tmpl is None:
        # Defensive: caller should never pass a missing strategy,
        # but treating it as cold_start beats raising.
        return ("cold_start", {})

    sweep_count, latest_completed = await _count_production_sweeps(
        db, strategy_id,
    )
    sample_count = tmpl.calibration_sample_count
    has_curve = bool(tmpl.calibration_curve)
    recent_brier, baseline_brier = await _split_brier_recent_baseline(
        db, strategy_id,
    )
    brier_ratio = (
        round(recent_brier / baseline_brier, 4)
        if recent_brier is not None
        and baseline_brier is not None
        and baseline_brier > 0
        else None
    )

    signals = {
        "production_sweep_count": sweep_count,
        "latest_sweep_completed_at": (
            latest_completed.isoformat() if latest_completed else None
        ),
        "calibration_sample_count": sample_count,
        "has_calibration_curve": has_curve,
        "recent_brier": (
            round(recent_brier, 4) if recent_brier is not None else None
        ),
        "baseline_brier": (
            round(baseline_brier, 4) if baseline_brier is not None else None
        ),
        "brier_ratio": brier_ratio,
    }

    # Stale check first — overrides everything except cold_start.
    # A strategy that USED to be mature but has been sitting unrun
    # for a month is signal-bearing in its own right; the operator
    # should see "stale" rather than the last known good tier.
    if latest_completed is not None:
        # SQLite returns naive datetimes even from `DateTime(timezone=True)`
        # columns. Coerce to UTC-aware so the subtraction works on
        # both backends.
        latest_aware = (
            latest_completed
            if latest_completed.tzinfo is not None
            else latest_completed.replace(tzinfo=UTC)
        )
        age = datetime.now(UTC) - latest_aware
        if age > timedelta(days=STALE_AFTER_DAYS):
            return ("stale", signals)

    if sweep_count == 0:
        return ("cold_start", signals)

    # Drifting before mature — a strategy that's accumulated 100
    # sweeps but recently degraded should NOT show up as mature.
    if brier_ratio is not None and brier_ratio >= DRIFT_RATIO_THRESHOLD:
        return ("drifting", signals)

    if (
        has_curve
        and (sample_count or 0) >= MIN_SAMPLES_FOR_MATURE
        and sweep_count >= MIN_PRODUCTION_SWEEPS_FOR_MATURE
    ):
        return ("mature", signals)

    return ("learning", signals)


async def update_maturity_tier(
    db: AsyncSession, strategy_id: UUID,
) -> tuple[str, dict[str, Any]]:
    """Compute + persist on the strategy template. Returns the
    same (tier, signals) tuple as `compute_maturity_tier`. Best-
    effort: persist failure logs but doesn't raise so the sweep
    worker's Phase 3 finalisation isn't blocked by classification.
    """
    tier, signals = await compute_maturity_tier(db, strategy_id)
    try:
        tmpl = await db.scalar(
            select(DiscussionStrategyTemplate).where(
                DiscussionStrategyTemplate.id == strategy_id,
            )
        )
        if tmpl is not None:
            tmpl.maturity_tier = tier
            tmpl.maturity_computed_at = datetime.now(UTC)
            await db.commit()
    except Exception as exc:
        log.warning(
            "strategy_maturity.persist_failed",
            extra={"strategy_id": str(strategy_id), "error": str(exc)},
        )
        try:
            await db.rollback()
        except Exception:
            pass
    return (tier, signals)


__all__ = [
    "MIN_SAMPLES_FOR_MATURE",
    "MIN_PRODUCTION_SWEEPS_FOR_MATURE",
    "DRIFT_RATIO_THRESHOLD",
    "STALE_AFTER_DAYS",
    "compute_maturity_tier",
    "update_maturity_tier",
]
