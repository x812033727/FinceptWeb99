"""Cross-strategy + cross-version comparison (PR-5c).

Two flavours:

  1. `compare_strategies(ids, days)` — aligns each strategy's
     daily health-metric series on a common date axis so the UI
     can plot them as overlay lines. Operator picks 2-4 strategies
     in the StrategyComparisonPage and sees their brier / hit-rate
     trajectories side by side.

  2. `compare_versions(strategy_id, v1, v2)` — diffs two specific
     `strategy_versions` rows (weights or calibration curves) and
     emits the per-key delta + sample-count delta + fit-time gap.
     For weights this is a diff dict; for calibration curves it's
     two control-point lists the UI overlays.

Both are pure reads. No writes.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric
from models.strategy_version import StrategyVersion

log = logging.getLogger(__name__)


MAX_COMPARE_STRATEGIES = 4


async def compare_strategies(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_ids: list[UUID],
    days: int = 90,
) -> dict[str, Any]:
    """Returns one entry per strategy with metric series + a few
    summary KPIs. Owner-scoped — strategies that don't belong to
    the caller are silently dropped (the API layer enforces 404
    when the entire request is invalid).

    Cap at `MAX_COMPARE_STRATEGIES` (4) to keep the chart legible
    and the response small. Excess IDs are truncated, not errored.
    """
    if not strategy_ids:
        return {"days": days, "strategies": []}
    days = max(int(days), 1)
    since = (datetime.now(UTC) - timedelta(days=days)).date()
    ids = list(strategy_ids)[:MAX_COMPARE_STRATEGIES]

    rows = list((await db.scalars(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id.in_(ids),
            DiscussionStrategyTemplate.owner_id == owner_id,
        )
    )).all())
    by_id = {r.id: r for r in rows}

    out: list[dict[str, Any]] = []
    for sid in ids:
        tmpl = by_id.get(sid)
        if tmpl is None:
            # Either doesn't exist or doesn't belong to this owner.
            # Skip silently — caller already passed the ID, no need
            # to surface "not found" since the rest of the response
            # is still meaningful.
            continue
        metric_rows = list((await db.scalars(
            select(StrategyHealthMetric)
            .where(
                StrategyHealthMetric.strategy_id == sid,
                StrategyHealthMetric.snapshot_date >= since,
            )
            .order_by(StrategyHealthMetric.snapshot_date.asc())
        )).all())

        # Roll up summary KPIs over the visible window
        briers = [
            float(r.brier_30d) for r in metric_rows
            if r.brier_30d is not None
        ]
        hits = [
            float(r.hit_rate_30d) for r in metric_rows
            if r.hit_rate_30d is not None
        ]
        out.append({
            "strategy_id": str(sid),
            "name": tmpl.name,
            "market": tmpl.market,
            "maturity_tier": tmpl.maturity_tier,
            "metrics": [
                {
                    "date": r.snapshot_date.isoformat(),
                    "brier_30d": r.brier_30d,
                    "calibrated_brier_30d": r.calibrated_brier_30d,
                    "hit_rate_30d": r.hit_rate_30d,
                    "sample_count": r.sample_count_30d,
                }
                for r in metric_rows
            ],
            "summary": {
                "brier_avg": (
                    round(sum(briers) / len(briers), 4) if briers else None
                ),
                "brier_latest": (
                    round(briers[-1], 4) if briers else None
                ),
                "hit_rate_avg": (
                    round(sum(hits) / len(hits), 4) if hits else None
                ),
                "hit_rate_latest": (
                    round(hits[-1], 4) if hits else None
                ),
                "sample_total": sum(
                    int(r.sample_count_30d or 0) for r in metric_rows
                ),
            },
        })
    return {"days": days, "strategies": out}


async def compare_versions(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
    v1_id: UUID,
    v2_id: UUID,
) -> dict[str, Any]:
    """Diff two version rows of the same strategy. Both versions
    must have the same `artifact_kind` (can't diff weights against
    a calibration curve)."""
    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
            DiscussionStrategyTemplate.owner_id == owner_id,
        )
    )
    if tmpl is None:
        raise ValueError("strategy not found or not owned by caller")

    v1 = await db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == v1_id,
            StrategyVersion.strategy_id == strategy_id,
        )
    )
    v2 = await db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == v2_id,
            StrategyVersion.strategy_id == strategy_id,
        )
    )
    if v1 is None or v2 is None:
        raise ValueError("version not found for this strategy")
    if v1.artifact_kind != v2.artifact_kind:
        raise ValueError(
            "version artifact_kind mismatch — can't compare "
            f"{v1.artifact_kind!r} vs {v2.artifact_kind!r}"
        )

    payload: dict[str, Any] = {
        "strategy_id": str(strategy_id),
        "artifact_kind": v1.artifact_kind,
        "v1": {
            "id": str(v1.id),
            "version_number": v1.version_number,
            "fit_at": v1.fit_at.isoformat() if v1.fit_at else None,
            "sample_count": v1.sample_count,
            "status": v1.status,
            "notes": v1.notes,
        },
        "v2": {
            "id": str(v2.id),
            "version_number": v2.version_number,
            "fit_at": v2.fit_at.isoformat() if v2.fit_at else None,
            "sample_count": v2.sample_count,
            "status": v2.status,
            "notes": v2.notes,
        },
    }

    if v1.artifact_kind == "weights":
        w1: dict[str, float] = dict(v1.payload or {})
        w2: dict[str, float] = dict(v2.payload or {})
        keys = set(w1) | set(w2)
        diff: dict[str, dict[str, float | None]] = {}
        for k in sorted(keys):
            try:
                a = float(w1[k]) if k in w1 else None
            except (TypeError, ValueError):
                a = None
            try:
                b = float(w2[k]) if k in w2 else None
            except (TypeError, ValueError):
                b = None
            delta = (b - a) if (a is not None and b is not None) else None
            diff[k] = {
                "v1": round(a, 4) if a is not None else None,
                "v2": round(b, 4) if b is not None else None,
                "delta": round(delta, 4) if delta is not None else None,
            }
        payload["weight_diff"] = diff
    elif v1.artifact_kind == "calibration_curve":
        # Curves: emit both control-point lists for the UI to overlay.
        payload["curve_v1"] = list(v1.payload or [])
        payload["curve_v2"] = list(v2.payload or [])

    return payload


__all__ = [
    "MAX_COMPARE_STRATEGIES",
    "compare_strategies",
    "compare_versions",
]
