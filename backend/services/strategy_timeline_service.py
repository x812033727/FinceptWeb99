"""Strategy lifecycle timeline assembly (PR-5a).

Merges multiple event sources into one chronological view so the
operator can see "what happened on this strategy" at a glance:

  - `strategy_health_metrics` snapshots (PR-4b) — daily brier /
    hit-rate / lesson-hit-rate dots forming the trend lines
  - `strategy_versions` rows (PR-4a) — every weights / curve fit
    becomes a `version_change` event with provenance notes
  - `backtest_sweeps` rows — every completed sweep becomes a
    `sweep_completed` event with fold_kind + discussion counts
  - tier transitions inferred from consecutive
    `maturity_tier_at_snapshot` values in the health series →
    `maturity_change` events
  - persona_status changes inferred from the strategy template's
    `persona_status_updated_at` → `persona_status_change` event

Pure read; no writes. The caller (UI) gets a single JSON payload
sorted chronologically that recharts can render directly.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric
from models.strategy_version import StrategyVersion

log = logging.getLogger(__name__)


def _isoformat_optional(value: Any) -> str | None:
    """Coerce datetime / date / None / str → str. SQLite returns
    naive datetimes for tz-aware columns; we accept either."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _detect_trigger_from_notes(notes: str | None) -> str:
    """Map the version row's free-text notes to a stable trigger
    enum the UI can switch on. Frontend marker colour keys off this."""
    if not notes:
        return "sweep_phase3"
    n = notes.lower()
    if "auto-promote" in n or "auto promote" in n:
        return "auto_promote"
    if "rolled back" in n or "rollback" in n:
        return "rollback"
    if "approved from pending" in n:
        return "calibration_approved"
    if "manual fit" in n:
        return "manual_fit"
    return "sweep_phase3"


async def _gather_health_series(
    db: AsyncSession, strategy_id: UUID, *, since: datetime,
) -> list[StrategyHealthMetric]:
    rows = list((await db.scalars(
        select(StrategyHealthMetric)
        .where(
            StrategyHealthMetric.strategy_id == strategy_id,
            StrategyHealthMetric.snapshot_date >= since.date(),
        )
        .order_by(StrategyHealthMetric.snapshot_date.asc())
    )).all())
    return rows


async def _gather_version_events(
    db: AsyncSession, strategy_id: UUID, *, since: datetime,
) -> list[dict[str, Any]]:
    rows = list((await db.scalars(
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.fit_at >= since,
        )
        .order_by(StrategyVersion.fit_at.asc())
    )).all())
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "date": _isoformat_optional(r.fit_at),
            "kind": "version_change",
            "artifact": r.artifact_kind,
            "version_number": r.version_number,
            "status": r.status,
            "sample_count": r.sample_count,
            "source_sweep_id": (
                str(r.source_sweep_id) if r.source_sweep_id else None
            ),
            "trigger": _detect_trigger_from_notes(r.notes),
            "notes": r.notes,
        })
    return out


async def _gather_sweep_events(
    db: AsyncSession, strategy_id: UUID, *, since: datetime,
) -> list[dict[str, Any]]:
    rows = list((await db.scalars(
        select(BacktestSweep)
        .where(
            BacktestSweep.strategy_id == strategy_id,
            BacktestSweep.completed_at.is_not(None),
            BacktestSweep.completed_at >= since,
        )
        .order_by(BacktestSweep.completed_at.asc())
    )).all())
    out: list[dict[str, Any]] = []
    for r in rows:
        completed_dates = list(r.completed_dates or [])
        failed_dates = list(r.failed_dates or [])
        out.append({
            "date": _isoformat_optional(r.completed_at),
            "kind": "sweep_completed",
            "sweep_id": str(r.id),
            "fold_kind": getattr(r, "fold_kind", None) or "production",
            "anchor_date": _isoformat_optional(r.anchor_date),
            "discussions_completed": len(completed_dates),
            "discussions_failed": len(failed_dates),
            "rounds_per_discussion": r.rounds_per_discussion,
        })
    return out


def _derive_maturity_change_events(
    health_rows: list[StrategyHealthMetric],
) -> list[dict[str, Any]]:
    """Walk the daily snapshots and emit one event per tier
    transition. The first-ever snapshot doesn't fire (there's no
    'from' to compare to) — only changes."""
    events: list[dict[str, Any]] = []
    prev_tier: str | None = None
    for row in health_rows:
        tier = row.maturity_tier_at_snapshot
        if tier and prev_tier and tier != prev_tier:
            events.append({
                "date": _isoformat_optional(row.snapshot_date),
                "kind": "maturity_change",
                "from": prev_tier,
                "to": tier,
            })
        if tier:
            prev_tier = tier
    return events


async def compose_timeline(
    db: AsyncSession,
    strategy_id: UUID,
    *,
    days: int = 90,
) -> dict[str, Any]:
    """Assemble the timeline payload. `days` clamps every source
    to the trailing window — typical UI calls pass 90.

    Empty list paths everywhere when the strategy has no data
    (cold-start, fresh template) so the UI always gets a
    well-shaped response.
    """
    days = max(int(days), 1)
    since = datetime.now(UTC) - timedelta(days=days)

    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
        )
    )
    if tmpl is None:
        return {
            "strategy_id": str(strategy_id),
            "days": days,
            "metrics": [],
            "events": [],
        }

    health_rows = await _gather_health_series(db, strategy_id, since=since)
    version_events = await _gather_version_events(
        db, strategy_id, since=since,
    )
    sweep_events = await _gather_sweep_events(
        db, strategy_id, since=since,
    )
    maturity_events = _derive_maturity_change_events(health_rows)

    persona_events: list[dict[str, Any]] = []
    ts = tmpl.persona_status_updated_at
    if ts is not None:
        # Only emit when the timestamp is within the window. SQLite
        # returns tz-naive even for tz-aware columns; coerce both
        # sides to compare safely.
        ts_aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        if ts_aware >= since:
            non_active = [
                pid for pid, status in (tmpl.persona_status or {}).items()
                if status != "active"
            ]
            persona_events.append({
                "date": _isoformat_optional(ts_aware),
                "kind": "persona_status_change",
                "non_active_personas": non_active,
                "non_active_count": len(non_active),
            })

    events = (
        version_events + sweep_events + maturity_events + persona_events
    )
    events.sort(key=lambda e: (e.get("date") or ""))

    metrics = [
        {
            "date": _isoformat_optional(r.snapshot_date),
            "brier_30d": r.brier_30d,
            "calibrated_brier_30d": r.calibrated_brier_30d,
            "hit_rate_30d": r.hit_rate_30d,
            "sample_count": r.sample_count_30d,
            "lesson_hit_rate_30d": r.lesson_hit_rate_30d,
            "maturity_tier": r.maturity_tier_at_snapshot,
            "status_flags": list(r.status_flags or []),
        }
        for r in health_rows
    ]

    return {
        "strategy_id": str(strategy_id),
        "days": days,
        "metrics": metrics,
        "events": events,
    }


__all__ = ["compose_timeline"]
