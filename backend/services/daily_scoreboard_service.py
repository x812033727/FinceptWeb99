"""Per-strategy scoreboard over the daily auto-run discussions.

The daily picking strategies are plain string keys on
`discussions.auto_run_strategy` (no template row, no sweep), so the
template-scoped `StrategyHealthMetric` pipeline never aggregates them.
This service is that missing rollup: group every verified auto-run
discussion by strategy and compute win rate, average realized 5-day
return of the picked symbols, and — where the verify task stored a
`pool_performance` snapshot — how the AI's picks did versus the whole
deterministic screener pool.

Pure aggregation over columns the verifier already persists
(`verdict`, `day1_open_prices`, `day5_close_prices`,
`pool_performance`); no OHLCV fetches. Retired strategy keys
(chip_momentum, ...) aggregate under their historical names on
purpose.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from models.discussion import Discussion

WINNING_VERDICTS = ("win", "big_win")
LOSING_VERDICTS = ("loss", "big_loss")


def _picked_return_pct(d: Discussion) -> float | None:
    """Realized average (D5 close / D1 open − 1) over the picked
    symbols, in percent. None when no symbol has both prices."""
    opens = d.day1_open_prices or {}
    closes = d.day5_close_prices or {}
    returns = [
        (closes[sym] / opens[sym] - 1.0) * 100.0
        for sym in closes
        if sym in opens and opens[sym] and opens[sym] > 0 and closes[sym] is not None
    ]
    if not returns:
        return None
    return sum(returns) / len(returns)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


async def build_scoreboard(
    db: AsyncSession, owner_id: Any,
) -> list[dict[str, Any]]:
    """One entry per strategy key, ordered by sample count descending
    (current keys first when tied alphabetically doesn't matter — the
    frontend maps names)."""
    rows = (
        await db.scalars(
            select(Discussion)
            .options(load_only(
                Discussion.id,
                Discussion.auto_run_strategy,
                Discussion.verdict,
                Discussion.day1_open_prices,
                Discussion.day5_close_prices,
                Discussion.pool_performance,
            ))
            .where(
                Discussion.owner_id == owner_id,
                Discussion.auto_run.is_(True),
                Discussion.status == "done",
                Discussion.verdict.is_not(None),
            )
        )
    ).all()

    grouped: dict[str, list[Discussion]] = {}
    for row in rows:
        grouped.setdefault(row.auto_run_strategy or "general", []).append(row)

    entries: list[dict[str, Any]] = []
    for strategy, group in grouped.items():
        wins = sum(1 for d in group if d.verdict in WINNING_VERDICTS)
        losses = sum(1 for d in group if d.verdict in LOSING_VERDICTS)
        decided = wins + losses

        pick_returns = [
            r for r in (_picked_return_pct(d) for d in group) if r is not None
        ]

        # AI-vs-pool alpha: only rows where BOTH sides resolved.
        alphas: list[float] = []
        pool_samples = 0
        for d in group:
            perf = d.pool_performance or {}
            pool_avg = perf.get("avg_return_pct")
            picked = _picked_return_pct(d)
            if pool_avg is None or picked is None:
                continue
            pool_samples += 1
            alphas.append(picked - pool_avg)

        entries.append({
            "strategy": strategy,
            "samples": len(group),
            "wins": wins,
            "losses": losses,
            "big_wins": sum(1 for d in group if d.verdict == "big_win"),
            "big_losses": sum(1 for d in group if d.verdict == "big_loss"),
            "unverifiable": sum(1 for d in group if d.verdict == "unverifiable"),
            "win_rate": round(wins / decided, 4) if decided else None,
            "avg_return_pct": _mean(pick_returns),
            "pool_samples": pool_samples,
            "avg_alpha_pct": _mean(alphas),
        })

    entries.sort(key=lambda e: (-e["samples"], e["strategy"]))
    return entries
