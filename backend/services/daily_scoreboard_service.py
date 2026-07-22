"""Per-strategy scoreboard over the daily auto-run discussions.

The daily picking strategies are plain string keys on
`discussions.auto_run_strategy` (no template row, no sweep), so the
template-scoped `StrategyHealthMetric` pipeline never aggregates them.
This service is that missing rollup: group every verified auto-run
discussion by strategy and compute win rate, average realized 5-day
return of the picked symbols, and — where the verify task stored a
`pool_performance` snapshot — how the AI's picks did versus the whole
deterministic screener pool.

Aggregation over columns the verifier already persists (`verdict`,
`day1_open_prices`, `day5_close_prices`, `pool_performance`), plus a
single archive read of the TAIEX total-return series for the
market-relative column. No upstream fetches. Retired strategy keys
(chip_momentum, ...) aggregate under their historical names on
purpose.

Three denominators are reported rather than one, because they answer
different questions and collapsing them is how a one-sample 100% win
rate ends up on a public page:
  - `decided`  = wins + losses; the win-rate denominator
  - `abstains` = the panel deliberately passed (a decision, not a gap)
  - `pending`  = window still open, not yet graded
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from models.discussion import Discussion
from models.ohlcv_daily import OhlcvDaily
from services.tw_trading_calendar import to_tw_date

WINNING_VERDICTS = ("win", "big_win")
LOSING_VERDICTS = ("loss", "big_loss")

# The 5-trading-day grading window, mirroring
# `tasks.verify_discussion_outcome._WINDOW_TRADING_DAYS`.
_WINDOW_TRADING_DAYS = 5
# Total-return TAIEX series archived by `tasks.ingest_taiex_tr_history`
# under a reserved (non-numeric) symbol in `ohlcv_daily`.
_BENCHMARK_SYMBOL = "_TAIEX_TR"


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


def _anchor_date(d: Discussion):
    """Entry date for the grading window — the same anchor the verifier
    uses (`as_of_date` in backtest mode, the Taipei calendar date of
    creation in live mode)."""
    return d.as_of_date or to_tw_date(d.created_at)


async def _load_benchmark_series(db: AsyncSession) -> list[tuple[Any, float, float]]:
    """`(session_date, open, close)` for the TAIEX total-return index,
    oldest first. One query; the series is ~1300 rows.

    Without a market benchmark the scoreboard can't answer the only
    question that matters in a drawdown: a +2% week reads as a win in
    isolation while the index did +5%, and as a strong call while the
    index did −7%.
    """
    rows = (
        await db.execute(
            select(OhlcvDaily.ts, OhlcvDaily.open, OhlcvDaily.close)
            .where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.symbol == _BENCHMARK_SYMBOL,
                OhlcvDaily.open.is_not(None),
                OhlcvDaily.close.is_not(None),
            )
            .order_by(OhlcvDaily.ts)
        )
    ).all()
    return [(_as_date(ts), float(o), float(c)) for ts, o, c in rows]


def _as_date(value: Any):
    return value.date() if hasattr(value, "date") else value


def _benchmark_return_pct(
    series: list[tuple[Any, float, float]], anchor: Any,
) -> float | None:
    """Index return over the same window a pick is graded on: first
    session open on/after `anchor` → close of the 5th session. None
    when the window hasn't completed yet (so a pending pick never
    gets compared against a truncated benchmark)."""
    window = [row for row in series if row[0] >= anchor][:_WINDOW_TRADING_DAYS]
    if len(window) < _WINDOW_TRADING_DAYS:
        return None
    entry_open = window[0][1]
    if entry_open <= 0:
        return None
    return (window[-1][2] / entry_open - 1.0) * 100.0


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
                Discussion.as_of_date,
                Discussion.created_at,
                Discussion.day1_open_prices,
                Discussion.day5_close_prices,
                Discussion.pool_performance,
            ))
            .where(
                Discussion.owner_id == owner_id,
                Discussion.auto_run.is_(True),
                Discussion.status == "done",
            )
        )
    ).all()

    benchmark = await _load_benchmark_series(db)

    grouped: dict[str, list[Discussion]] = {}
    for row in rows:
        grouped.setdefault(row.auto_run_strategy or "general", []).append(row)

    entries: list[dict[str, Any]] = []
    for strategy, group in grouped.items():
        wins = sum(1 for d in group if d.verdict in WINNING_VERDICTS)
        losses = sum(1 for d in group if d.verdict in LOSING_VERDICTS)
        decided = wins + losses
        # `abstain` is a real decision, not a missing one, so it is
        # reported separately and deliberately kept OUT of the win-rate
        # denominator — otherwise a strategy that correctly sat out a
        # falling tape would be punished as if it had lost.
        abstains = sum(1 for d in group if d.verdict == "abstain")
        # Not yet graded (window still open). Surfacing this is the
        # whole point of C3: a 100% win rate over one decided sample
        # with three still pending is not a 100% win rate.
        pending = sum(1 for d in group if d.verdict is None)

        pick_returns = [
            r for r in (_picked_return_pct(d) for d in group) if r is not None
        ]

        # AI-vs-pool alpha: only rows where BOTH sides resolved.
        alphas: list[float] = []
        pool_samples = 0
        # AI-vs-market excess: same window, TAIEX total-return index.
        excesses: list[float] = []
        benchmark_samples = 0
        for d in group:
            picked = _picked_return_pct(d)
            perf = d.pool_performance or {}
            pool_avg = perf.get("avg_return_pct")
            if pool_avg is not None and picked is not None:
                pool_samples += 1
                alphas.append(picked - pool_avg)
            if picked is not None:
                index_return = _benchmark_return_pct(benchmark, _anchor_date(d))
                if index_return is not None:
                    benchmark_samples += 1
                    excesses.append(picked - index_return)

        entries.append({
            "strategy": strategy,
            "samples": len(group),
            "decided": decided,
            "pending": pending,
            "wins": wins,
            "losses": losses,
            "big_wins": sum(1 for d in group if d.verdict == "big_win"),
            "big_losses": sum(1 for d in group if d.verdict == "big_loss"),
            "abstains": abstains,
            "unverifiable": sum(1 for d in group if d.verdict == "unverifiable"),
            "win_rate": round(wins / decided, 4) if decided else None,
            "avg_return_pct": _mean(pick_returns),
            "pool_samples": pool_samples,
            "avg_alpha_pct": _mean(alphas),
            "benchmark_samples": benchmark_samples,
            "avg_excess_vs_taiex_pct": _mean(excesses),
        })

    entries.sort(key=lambda e: (-e["samples"], e["strategy"]))
    return entries
