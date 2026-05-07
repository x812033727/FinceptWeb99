"""Per-persona performance roll-up for the leaderboard UI (PR-5b).

PR-4 series produced the data sources (turns, verdicts, version
history, persona_status); this service aggregates them per persona
so the operator can see "who's pulling their weight" at a glance.

Cheap aggregations only:
  - `participation_count` — count of `discussion_turns` rows in the
    rolling window
  - `win_attribution_count` / `_rate` — discussions where this
    persona spoke AND verdict resolved to 'win'
  - `average_weight` / `weight_trend_30d` — only when a `strategy_id`
    is supplied (otherwise persona weights aren't comparable since
    they're per-strategy)
  - `status_in_strategies` — counts of frozen / shadow assignments
    across the owner's strategies

Citation / hallucination rates are intentionally left to
`SignalAuditCard` to compute. Joining them in here would require
walking every TurnAudit per persona which doesn't pay off for the
leaderboard's "rank order" use case.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_version import StrategyVersion

log = logging.getLogger(__name__)


WEIGHT_TREND_RECENT_N = 3
WEIGHT_TREND_BASELINE_N = 5


async def _gather_persona_participation(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID | None,
    market: str | None,
    since: datetime,
) -> tuple[
    dict[str, int],         # participation: persona_id → count
    dict[str, int],         # win_attribution: persona_id → count
    int,                    # total discussions in window
]:
    """Walk discussions in the window, count per-persona
    participation + win attribution.

    Optimization: we don't need the full Turn rows — just the
    distinct (discussion_id, persona_id) pairs, joined with the
    discussion's verdict column. SQLAlchemy's expression API gives
    us that in two queries instead of N+1.
    """
    base = select(Discussion).where(
        Discussion.owner_id == owner_id,
        Discussion.created_at >= since,
    )
    if market:
        base = base.where(Discussion.market == market)
    if strategy_id is not None:
        # When strategy is scoped, only count discussions spawned
        # by sweeps of that strategy.
        from models.backtest_sweep import BacktestSweep
        sweep_ids = list((await db.scalars(
            select(BacktestSweep.id).where(
                BacktestSweep.strategy_id == strategy_id,
            )
        )).all())
        if not sweep_ids:
            return ({}, {}, 0)
        base = base.where(Discussion.sweep_id.in_(sweep_ids))

    discussions = list((await db.scalars(base)).all())
    if not discussions:
        return ({}, {}, 0)
    by_id = {d.id: d for d in discussions}

    # Collect distinct (discussion_id, persona_id) pairs from turns.
    # User-input directive turns (USER_PERSONA_ID '_user') aren't
    # personas; filter them out.
    turn_pairs = list((await db.execute(
        select(DiscussionTurn.discussion_id, DiscussionTurn.persona_id)
        .where(
            DiscussionTurn.discussion_id.in_(by_id.keys()),
            DiscussionTurn.persona_id != "_user",
        )
        .distinct()
    )).all())

    participation: dict[str, int] = defaultdict(int)
    win_attribution: dict[str, int] = defaultdict(int)
    for d_id, persona_id in turn_pairs:
        participation[persona_id] += 1
        verdict = by_id[d_id].verdict if d_id in by_id else None
        if verdict == "win":
            win_attribution[persona_id] += 1
    return (dict(participation), dict(win_attribution), len(discussions))


async def _gather_per_strategy_weight_stats(
    db: AsyncSession,
    *,
    strategy_id: UUID,
) -> tuple[dict[str, float], dict[str, float]]:
    """Compute per-persona average + trend over the most recent
    weight versions. Returns:
      ({persona_id: avg_weight}, {persona_id: trend_delta})
    Trend = avg(latest 3) - avg(prior 5). Positive = persona is
    gaining weight; negative = losing.
    """
    rows = list((await db.scalars(
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.artifact_kind == "weights",
        )
        .order_by(StrategyVersion.fit_at.desc())
        .limit(WEIGHT_TREND_RECENT_N + WEIGHT_TREND_BASELINE_N)
    )).all())
    if not rows:
        return ({}, {})
    # rows is newest-first
    recent = rows[:WEIGHT_TREND_RECENT_N]
    baseline = rows[WEIGHT_TREND_RECENT_N:]
    avg_weights: dict[str, list[float]] = defaultdict(list)
    recent_weights: dict[str, list[float]] = defaultdict(list)
    baseline_weights: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        for pid, w in (r.payload or {}).items():
            try:
                avg_weights[pid].append(float(w))
            except (TypeError, ValueError):
                continue
    for r in recent:
        for pid, w in (r.payload or {}).items():
            try:
                recent_weights[pid].append(float(w))
            except (TypeError, ValueError):
                continue
    for r in baseline:
        for pid, w in (r.payload or {}).items():
            try:
                baseline_weights[pid].append(float(w))
            except (TypeError, ValueError):
                continue
    avgs = {pid: sum(vs) / len(vs) for pid, vs in avg_weights.items()}
    trends: dict[str, float] = {}
    for pid in avg_weights:
        rec = recent_weights.get(pid, [])
        base = baseline_weights.get(pid, [])
        if rec and base:
            trends[pid] = (sum(rec) / len(rec)) - (sum(base) / len(base))
    return (avgs, trends)


async def _gather_status_counts(
    db: AsyncSession,
    *,
    owner_id: UUID,
) -> dict[str, dict[str, int]]:
    """Across all of this owner's strategies, count how many have
    each persona in `frozen` / `shadow` state."""
    strategies = list((await db.scalars(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.owner_id == owner_id,
            DiscussionStrategyTemplate.deleted_at.is_(None),
        )
    )).all())
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"frozen": 0, "shadow": 0},
    )
    for s in strategies:
        for pid, status in (s.persona_status or {}).items():
            if status in ("frozen", "shadow"):
                counts[pid][status] += 1
    return dict(counts)


async def compute_persona_performance(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID | None = None,
    market: str | None = None,
    days: int = 90,
) -> list[dict[str, Any]]:
    """Return one row per persona that participated in the window.

    Sorted by `win_attribution_rate` descending then `participation_
    count` descending — pulls the most consistent contributors to
    the top of the list.
    """
    days = max(int(days), 1)
    since = datetime.now(UTC) - timedelta(days=days)

    participation, wins, total = await _gather_persona_participation(
        db, owner_id=owner_id, strategy_id=strategy_id,
        market=market, since=since,
    )

    avg_weights: dict[str, float] = {}
    weight_trends: dict[str, float] = {}
    if strategy_id is not None:
        avg_weights, weight_trends = await _gather_per_strategy_weight_stats(
            db, strategy_id=strategy_id,
        )

    status_counts = await _gather_status_counts(db, owner_id=owner_id)

    out: list[dict[str, Any]] = []
    persona_ids = set(participation) | set(avg_weights) | set(status_counts)
    for pid in persona_ids:
        p_count = int(participation.get(pid, 0))
        w_count = int(wins.get(pid, 0))
        rate = (w_count / p_count) if p_count > 0 else None
        sc = status_counts.get(pid, {"frozen": 0, "shadow": 0})
        out.append({
            "persona_id": pid,
            "participation_count": p_count,
            "win_attribution_count": w_count,
            "win_attribution_rate": (
                round(rate, 4) if rate is not None else None
            ),
            "average_weight": (
                round(avg_weights[pid], 4) if pid in avg_weights else None
            ),
            "weight_trend_30d": (
                round(weight_trends[pid], 4) if pid in weight_trends else None
            ),
            "frozen_in_strategies": sc.get("frozen", 0),
            "shadow_in_strategies": sc.get("shadow", 0),
        })
    out.sort(
        key=lambda r: (
            -(r["win_attribution_rate"] or -1.0),
            -(r["participation_count"] or 0),
        )
    )
    return out


__all__ = [
    "WEIGHT_TREND_RECENT_N",
    "WEIGHT_TREND_BASELINE_N",
    "compute_persona_performance",
]
