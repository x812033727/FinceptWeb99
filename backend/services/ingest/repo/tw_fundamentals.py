"""TW fundamentals: monthly revenue, buyback, risk signals, fundamentals snapshots."""
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import or_ as sa_or
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.fundamentals_snapshot import FundamentalsSnapshot
from models.tw_chip_metrics import TwMarginDaily
from models.tw_revenue_monthly import TwRevenueMonthly
from models.tw_risk_signals import (
    TwStockDayTradingDaily,
    TwStockDisposition,
    TwStockSuspended,
)
from models.tw_stock_buyback import TwStockBuyback
from services.ingest.repo._common import _chunked_upsert

log = logging.getLogger(__name__)


# ── TW monthly revenue ─────────────────────────────────────────────


@dataclass(frozen=True)
class RevenueMonthlyRow:
    """One company's monthly revenue + growth percentages."""
    market: str
    symbol: str
    ts: date
    revenue: int | None
    revenue_yoy: float | None
    revenue_mom: float | None
    source: str


_REVENUE_FIELDS = (
    "market", "symbol", "ts",
    "revenue", "revenue_yoy", "revenue_mom",
    "source",
)


async def upsert_revenue_monthly(
    db: AsyncSession, rows: Iterable[RevenueMonthlyRow],
) -> int:
    """Bulk upsert. ON CONFLICT (market, symbol, ts) overwrites the
    metric columns + source — re-pulling the same month after a
    correction by FinMind / TWSE replaces the stale values."""
    payload = [
        {
            "market":      r.market,
            "symbol":      r.symbol,
            "ts":          r.ts,
            "revenue":     r.revenue,
            "revenue_yoy": r.revenue_yoy,
            "revenue_mom": r.revenue_mom,
            "source":      r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwRevenueMonthly,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=("revenue", "revenue_yoy", "revenue_mom", "source"),
    )


def _revenue_row_out(r: TwRevenueMonthly) -> dict[str, Any]:
    """Output shape mirrors the FinMind connector — drop-in for
    `tw_market_service.get_revenue` callers.

    PR #215 defensive filter: rows carrying the PR #211 bogus
    signature (yoy = year(ts) AND mom = month(ts)) get yoy/mom set
    to 0.0 — same as the legitimate "no baseline" path. Stops the
    +2026% number leaking out of the per-symbol revenue page."""
    yoy = float(r.revenue_yoy) if r.revenue_yoy is not None else 0.0
    mom = float(r.revenue_mom) if r.revenue_mom is not None else 0.0
    if _is_bogus_growth_pair(r.revenue_yoy, r.revenue_mom, r.ts):
        yoy = 0.0
        mom = 0.0
    return {
        "date":        r.ts.isoformat(),
        "symbol":      r.symbol,
        "revenue":     int(r.revenue) if r.revenue is not None else 0,
        "revenue_yoy": yoy,
        "revenue_mom": mom,
    }


async def read_revenue_range(
    db: AsyncSession, market: str, symbol: str, start: date, end: date,
) -> list[dict[str, Any]]:
    stmt = (
        select(TwRevenueMonthly)
        .where(
            TwRevenueMonthly.market == market,
            TwRevenueMonthly.symbol == symbol,
            TwRevenueMonthly.ts >= start,
            TwRevenueMonthly.ts <= end,
        )
        .order_by(TwRevenueMonthly.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [_revenue_row_out(r) for r in rows]


def _is_bogus_growth_pair(
    yoy: float | None, mom: float | None, ts: date,
) -> bool:
    """Detect the PR #211 bug signature: rows where `revenue_yoy` is
    the period year integer (e.g. 2026) and `revenue_mom` is the
    period month integer (e.g. 1), because the FinMind connector
    earlier mis-mapped its `revenue_year` / `revenue_month` fields as
    growth percentages.

    Filters at the read layer rather than the write layer so cleanup
    works without a DB migration.

    Two signatures, in decreasing strictness:

      1. Strict (PR #215): BOTH ``yoy == ts.year`` AND ``mom == ts.month``.
         Originally the only check, since the FinMind bug always leaked
         the (year, month) pair together.

      2. Looser (PR #231): ``yoy == ts.year`` alone, when ``yoy`` is an
         exact integer. Production data showed rows where partial
         cleanup nulled ``mom`` but left the bogus ``yoy`` (e.g. 1101
         showing ``YoY +2026.0%`` in 2026-XX), bypassing the strict
         filter. ``_pct_change`` rounds to 4 dp on integer revenue
         values, so an actual computed growth rate essentially never
         lands on a clean integer — let alone exactly the period year.
         False-positive risk: a real company growing exactly N% YoY
         where N == period year would be nulled. Probability is
         vanishingly small (would need ~21x revenue growth in one
         month, AND the integer rate would need zero fractional
         component despite being computed from arbitrary integer
         revenues).
    """
    if yoy is None:
        return False
    try:
        yoy_f = float(yoy)
    except (TypeError, ValueError):
        return False
    # Strict signature first.
    if mom is not None:
        try:
            if yoy_f == float(ts.year) and float(mom) == float(ts.month):
                return True
        except (TypeError, ValueError):
            pass
    # Looser signature: bogus yoy alone, even when mom was scrubbed.
    if yoy_f == float(ts.year) and yoy_f == int(yoy_f):
        return True
    return False


async def read_top_revenue_growers(
    db: AsyncSession,
    market: str = "TW",
    *,
    limit: int = 10,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Top-N TW symbols by latest reported month's YoY revenue growth.

    "Latest month" = the most recent `ts <= as_of` (default: latest
    available) present for `market` in `tw_revenue_monthly`. Returns
    an empty list when the archive has nothing for the market. Skips
    rows with NULL `revenue_yoy` so a company that just IPO'd (no
    prior year baseline) doesn't push a real grower off the list.

    PR #215 also defensively filters out rows whose growth pair
    matches `(year, month)` of the row's `ts` — the bug signature
    from PR #211 where the FinMind connector mis-mapped
    `revenue_year` / `revenue_month` integers as growth percentages.
    Production rows written before the fix shipped still carry that
    pattern, and the daily cron is paywalled so it can't overwrite
    them. This filter makes the LLM stop seeing fabricated +2026%
    YoY without waiting on a `backfill_revenue_finmind` run.
    """
    latest_stmt = select(TwRevenueMonthly.ts).where(
        TwRevenueMonthly.market == market,
    )
    if as_of is not None:
        latest_stmt = latest_stmt.where(TwRevenueMonthly.ts <= as_of)
    latest_stmt = latest_stmt.order_by(TwRevenueMonthly.ts.desc()).limit(1)
    latest = await db.scalar(latest_stmt)
    if latest is None:
        return []
    target = latest

    stmt = (
        select(TwRevenueMonthly)
        .where(
            TwRevenueMonthly.market == market,
            TwRevenueMonthly.ts == target,
            TwRevenueMonthly.revenue_yoy.isnot(None),
        )
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        return []

    # Drop the PR #211 bogus signature before ranking. Without this,
    # the bogus rows (yoy = 2026 etc.) sort to the top and dominate
    # the "top growers" list.
    rows = [
        r for r in rows
        if not _is_bogus_growth_pair(
            r.revenue_yoy, r.revenue_mom, r.ts,
        )
    ]
    if not rows:
        return []

    rows_sorted = sorted(
        rows,
        key=lambda r: float(r.revenue_yoy) if r.revenue_yoy is not None else -1e9,
        reverse=True,
    )
    # Backtest look-ahead defense (PR #256): the ingest task's
    # `update_cols` includes `revenue_yoy` and `revenue_mom`, so a
    # later backfill cron can RECOMPUTE these against revised
    # baseline data. A backtest reading at as_of would then see the
    # post-revision number, not what was public on as_of. Mask the
    # exact values in backtest mode while keeping the ranking
    # itself (the SET of top growers is stable across minor
    # restatements; the precise number isn't). Live mode keeps the
    # values as-is — the leak only matters when reconstructing a
    # past anchor.
    is_backtest = as_of is not None
    return [
        {
            "symbol":      r.symbol,
            "ts":          r.ts.isoformat(),
            "revenue":     int(r.revenue) if r.revenue is not None else 0,
            # Backtest mode masks yoy/mom to None — the row's
            # presence in the top-N already conveys the actionable
            # info (this is one of the highest-growth names);
            # exposing the precise number would leak post-as_of
            # restatements.
            "revenue_yoy": (
                None if is_backtest
                else (float(r.revenue_yoy) if r.revenue_yoy is not None else None)
            ),
            "revenue_mom": (
                None if is_backtest
                else (float(r.revenue_mom) if r.revenue_mom is not None else None)
            ),
        }
        for r in rows_sorted[:limit]
    ]


async def read_market_margin_balance_trend(
    db: AsyncSession,
    market: str = "TW",
    *,
    days: int = 5,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Aggregate market-wide margin balance over the last `days` ending
    at `as_of` (default: today). Used by the discussion context as a
    leverage / retail-activity proxy ("散戶融資餘額連續 X 日創高")."""
    end = as_of or date.today()
    start = end - timedelta(days=days * 2)
    stmt = (
        select(TwMarginDaily)
        .where(
            TwMarginDaily.market == market,
            TwMarginDaily.ts >= start,
            TwMarginDaily.ts <= end,
        )
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        return {
            "as_of": None,
            "total_margin_balance": 0,
            "total_short_balance": 0,
            "days_observed": 0,
        }
    by_date: dict[date, dict[str, int]] = {}
    for r in rows:
        bucket = by_date.setdefault(
            r.ts, {"margin_balance": 0, "short_balance": 0},
        )
        bucket["margin_balance"] += int(r.margin_balance or 0)
        bucket["short_balance"] += int(r.short_balance or 0)
    if not by_date:
        return {
            "as_of": None,
            "total_margin_balance": 0,
            "total_short_balance": 0,
            "days_observed": 0,
        }
    latest_date = max(by_date.keys())
    latest = by_date[latest_date]
    return {
        "as_of": latest_date.isoformat(),
        "total_margin_balance": latest["margin_balance"],
        "total_short_balance": latest["short_balance"],
        "days_observed": len(by_date),
    }


# ── TW buyback announcements (庫藏股) ─────────────────────────────

@dataclass(frozen=True)
class BuybackRow:
    """One company's single buyback announcement. `current_shares`
    is the latest execution figure (FinMind updates this daily as
    the company prints fills); the cron's UPSERT overwrites in
    place so reads always see the latest progress for an active
    round."""
    market: str
    symbol: str
    announce_date: date
    period_start: date | None
    period_end: date | None
    method: int | None
    purpose: str | None
    max_shares: int | None
    current_shares: int | None
    price_lower: float | None
    price_upper: float | None
    source: str


async def upsert_buybacks(
    db: AsyncSession, rows: Iterable[BuybackRow],
) -> int:
    """Bulk upsert keyed on (market, symbol, announce_date).
    Re-pulling the same announcement during its execution window
    overwrites `current_shares` + period fields with the latest
    upstream value."""
    payload = [
        {
            "market":         r.market,
            "symbol":         r.symbol,
            "announce_date":  r.announce_date,
            "period_start":   r.period_start,
            "period_end":     r.period_end,
            "method":         r.method,
            "purpose":        r.purpose,
            "max_shares":     r.max_shares,
            "current_shares": r.current_shares,
            "price_lower":    r.price_lower,
            "price_upper":    r.price_upper,
            "source":         r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockBuyback,
        payload=payload,
        index_elements=["market", "symbol", "announce_date"],
        update_cols=(
            "period_start", "period_end", "method", "purpose",
            "max_shares", "current_shares", "price_lower", "price_upper",
            "source",
        ),
    )


async def read_active_buybacks(
    db: AsyncSession, *, market: str = "TW", as_of: date | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Buybacks whose execution window contains `as_of` (default:
    today). Used by the discussion context aggregator to surface
    "公司自家正在買回" as a bullish signal block, plus by the
    StockDetailPage company-info card.

    Sorted by `announce_date` desc — the freshest announcement first
    so the discussion context block reads as "newest moves up top".
    A row missing `period_end` is treated as still-active out to
    365 days from announcement (FinMind sometimes leaves the upstream
    field blank for very recent filings).
    """
    today = as_of or date.today()
    fallback_end = today  # rows with NULL period_end fall back to "still open"
    stmt = (
        select(TwStockBuyback)
        .where(
            TwStockBuyback.market == market,
            TwStockBuyback.announce_date <= today,
            sa_or(
                TwStockBuyback.period_end.is_(None),
                TwStockBuyback.period_end >= fallback_end,
            ),
        )
        .order_by(TwStockBuyback.announce_date.desc())
        .limit(limit)
    )
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "symbol":         r.symbol,
            "announce_date":  r.announce_date.isoformat(),
            "period_start":   r.period_start.isoformat() if r.period_start else None,
            "period_end":     r.period_end.isoformat() if r.period_end else None,
            "method":         r.method,
            "purpose":        r.purpose,
            "max_shares":     int(r.max_shares) if r.max_shares is not None else None,
            "current_shares": int(r.current_shares) if r.current_shares is not None else None,
            "price_lower":    float(r.price_lower) if r.price_lower is not None else None,
            "price_upper":    float(r.price_upper) if r.price_upper is not None else None,
        }
        for r in rows
    ]


# ── 風險警示三件套 (PR #192) ──────────────────────────────────────
#
# Three small archives ingested together by `tasks.ingest_risk_signals_tw`
# for the discussion-context "risk warnings" block. Each has its own
# upsert + a read helper that aggregates the way the discussion
# aggregator wants (counts + sample symbols, not raw rows).


@dataclass(frozen=True)
class DispositionRow:
    market: str
    symbol: str
    period_start: date
    period_end: date | None
    classification: str | None
    level: int | None
    reason: str | None
    source: str


async def upsert_dispositions(
    db: AsyncSession, rows: Iterable[DispositionRow],
) -> int:
    payload = [
        {
            "market":         r.market,
            "symbol":         r.symbol,
            "period_start":   r.period_start,
            "period_end":     r.period_end,
            "classification": r.classification,
            "level":          r.level,
            "reason":         r.reason,
            "source":         r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockDisposition,
        payload=payload,
        index_elements=["market", "symbol", "period_start"],
        update_cols=("period_end", "classification", "level", "reason", "source"),
    )


async def read_active_dispositions(
    db: AsyncSession, *, market: str = "TW", as_of: date | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Stocks whose disposition window covers `as_of`. NULL
    period_end is treated as still-active (FinMind sometimes leaves
    the upstream field blank for fresh announcements).

    Backtest look-ahead protection (PR #243): when `as_of` is set,
    `period_end` is masked to None in the output. Rationale: the
    upsert's `update_cols` includes `period_end`, so a disposition
    announced on 03-23 with original end 03-30 that later got
    extended to 04-07 (re-ingested by a subsequent FinMind tick)
    will have its DB row's period_end overwritten to 04-07. A
    backtest reading at as_of=03-23 would then see 04-07, which
    leaks the future extension that wasn't public on 03-23.
    Personas only need the LIST of currently-disposed stocks (the
    action is "avoid until period ends" regardless), so dropping
    the precise end date eliminates the leak without losing
    decision-relevant information.

    Live mode (`as_of=None`) returns period_end as-is — the leak
    only matters when reconstructing a past anchor.
    """
    today = as_of or date.today()
    stmt = (
        select(TwStockDisposition)
        .where(
            TwStockDisposition.market == market,
            TwStockDisposition.period_start <= today,
            sa_or(
                TwStockDisposition.period_end.is_(None),
                TwStockDisposition.period_end >= today,
            ),
        )
        .order_by(TwStockDisposition.period_start.desc())
        .limit(limit)
    )
    rows = (await db.scalars(stmt)).all()
    is_backtest = as_of is not None
    return [
        {
            "symbol":         r.symbol,
            "period_start":   r.period_start.isoformat(),
            # Backtest: hide period_end to defang the FinMind-extension
            # look-ahead. Live mode shows the real value.
            "period_end": (
                None if is_backtest
                else (r.period_end.isoformat() if r.period_end else None)
            ),
            "classification": r.classification,
            "level":          r.level,
            "reason":         r.reason,
        }
        for r in rows
    ]


@dataclass(frozen=True)
class SuspendedRow:
    market: str
    symbol: str
    ts: date
    status: str | None
    reason: str | None
    source: str


async def upsert_suspensions(
    db: AsyncSession, rows: Iterable[SuspendedRow],
) -> int:
    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "ts":     r.ts,
            "status": r.status,
            "reason": r.reason,
            "source": r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockSuspended,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=("status", "reason", "source"),
    )


async def read_recent_suspensions(
    db: AsyncSession, *, market: str = "TW", days: int = 7,
    limit: int = 30, as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Suspensions whose `ts` falls in the `[end-days, end]` window.

    Backtest look-ahead protection (PR #260): when `as_of` is set,
    `status` and `reason` are masked to None in the output.
    Rationale: ``upsert_suspensions``'s ``update_cols`` includes
    both fields, so a row originally inserted with
    ``status="halt"`` whose status is later overwritten to
    ``"lifted"`` (next ingest tick) would, at backtest read-time,
    leak the post-as_of recovery state. The persona only needs the
    LIST of suspended stocks (action: "avoid until cleared"),
    which the row's presence already conveys; dropping the precise
    status / reason eliminates the leak without losing
    decision-relevant information.

    Live mode (``as_of=None``) returns both fields as-is — the leak
    only matters when reconstructing a past anchor.
    """
    end = as_of or date.today()
    cutoff = end - timedelta(days=days)
    stmt = (
        select(TwStockSuspended)
        .where(
            TwStockSuspended.market == market,
            TwStockSuspended.ts >= cutoff,
            TwStockSuspended.ts <= end,
        )
        .order_by(TwStockSuspended.ts.desc())
        .limit(limit)
    )
    rows = (await db.scalars(stmt)).all()
    is_backtest = as_of is not None
    return [
        {
            "symbol": r.symbol,
            "date":   r.ts.isoformat(),
            # Backtest: mask retroactively-updatable fields. Live
            # mode shows the real values.
            "status": None if is_backtest else r.status,
            "reason": None if is_backtest else r.reason,
        }
        for r in rows
    ]


@dataclass(frozen=True)
class DayTradingRow:
    market: str
    symbol: str
    ts: date
    volume: int | None
    buy_amount: int | None
    sell_amount: int | None
    source: str


async def upsert_day_trading(
    db: AsyncSession, rows: Iterable[DayTradingRow],
) -> int:
    payload = [
        {
            "market":      r.market,
            "symbol":      r.symbol,
            "ts":          r.ts,
            "volume":      r.volume,
            "buy_amount":  r.buy_amount,
            "sell_amount": r.sell_amount,
            "source":      r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockDayTradingDaily,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=("volume", "buy_amount", "sell_amount", "source"),
    )


async def read_high_day_trading_ratio(
    db: AsyncSession, *, market: str = "TW", days: int = 1,
    threshold: float = 0.6, limit: int = 20,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Stocks where day-trading turnover dominates regular trading
    over the last `days` sessions ending at `as_of` (default: today).
    The "ratio" is computed as `(buy_amount + sell_amount) / 2 /
    volume` — each side counted once because a day-trade is one
    round-trip. A ratio > 0.5 means the majority of volume was
    intraday round-trips, which is a speculative-character signal.
    """
    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 3)  # weekend slack
    stmt = (
        select(TwStockDayTradingDaily)
        .where(
            TwStockDayTradingDaily.market == market,
            TwStockDayTradingDaily.ts >= cutoff,
            TwStockDayTradingDaily.ts <= end,
        )
        .order_by(TwStockDayTradingDaily.ts.desc())
    )
    rows = (await db.scalars(stmt)).all()
    # Group by symbol, take latest non-zero-volume day, compute ratio.
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.symbol in seen:
            continue
        vol = int(r.volume or 0)
        if vol <= 0:
            continue
        side = (int(r.buy_amount or 0) + int(r.sell_amount or 0)) / 2
        ratio = side / vol if vol > 0 else 0.0
        if ratio < threshold:
            continue
        seen[r.symbol] = {
            "symbol": r.symbol,
            "date":   r.ts.isoformat(),
            "ratio":  round(ratio, 4),
            "volume": vol,
        }
    out = list(seen.values())
    out.sort(key=lambda d: d["ratio"], reverse=True)
    return out[:limit]


async def read_symbol_day_trading_trend(
    db: AsyncSession, *, market: str = "TW", symbol: str,
    days: int = 5, as_of: date | None = None,
) -> dict[str, Any] | None:
    """Per-symbol day-trading ratio trend over the last `days` sessions.

    Distinct from `read_high_day_trading_ratio`, which returns the
    market-wide list of stocks above a single-day threshold (typically
    > 0.6). This per-symbol read tracks the rolling intensity for one
    focus stock — a 0.5 ratio that's been climbing for 3 sessions
    behaves very differently from a one-day spike.

    Used by the per-symbol short-term signals technical block so
    personas have intraday-speculation context for every focus
    symbol, not just the > 60% market-wide bucket.

    Returns:
        {
            "latest_ratio": float,         # most recent session
            "latest_date":  str,           # iso date of latest session
            "mean_ratio":   float,         # mean over the lookback
            "session_count": int,          # sessions actually present
            "trend":        "rising" | "stable" | "falling",
        }
        Or None when no sessions exist for the symbol in the window
        (fresh listing, gap in ingest, or `as_of` predates the table).

    `trend` heuristic: latest > mean * 1.10 → "rising",
                       latest < mean * 0.90 → "falling",
                       otherwise            → "stable".
    """
    end = as_of or date.today()
    # `days * 3` covers weekends + a holiday cluster while still
    # converging to ~`days` real sessions in the typical month.
    cutoff = end - timedelta(days=max(days * 3, 14))
    stmt = (
        select(TwStockDayTradingDaily)
        .where(
            TwStockDayTradingDaily.market == market,
            TwStockDayTradingDaily.symbol == symbol,
            TwStockDayTradingDaily.ts >= cutoff,
            TwStockDayTradingDaily.ts <= end,
        )
        .order_by(TwStockDayTradingDaily.ts.desc())
        .limit(days)
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        return None

    ratios: list[tuple[date, float]] = []
    for r in rows:
        vol = int(r.volume or 0)
        if vol <= 0:
            continue
        side = (int(r.buy_amount or 0) + int(r.sell_amount or 0)) / 2
        ratios.append((r.ts, side / vol))
    if not ratios:
        return None

    latest_date, latest_ratio = ratios[0]   # ordered desc
    mean_ratio = sum(r for _, r in ratios) / len(ratios)
    if mean_ratio == 0:
        trend = "stable"
    elif latest_ratio > mean_ratio * 1.10:
        trend = "rising"
    elif latest_ratio < mean_ratio * 0.90:
        trend = "falling"
    else:
        trend = "stable"

    return {
        "latest_ratio":  round(latest_ratio, 4),
        "latest_date":   latest_date.isoformat(),
        "mean_ratio":    round(mean_ratio, 4),
        "session_count": len(ratios),
        "trend":         trend,
    }


# ── Fundamentals snapshots ─────────────────────────────────────────

@dataclass(frozen=True)
class FundamentalsSnapshotRow:
    market: str
    symbol: str
    as_of: date
    pe_ratio: float | None
    pb_ratio: float | None
    dividend_yield: float | None
    eps: float | None
    revenue: float | None
    payload: dict[str, Any] | None
    source: str


async def upsert_fundamentals_snapshots(
    db: AsyncSession, rows: Iterable[FundamentalsSnapshotRow],
) -> int:
    """Bulk upsert. Returns number of input rows.

    Re-running same day overwrites the row's price columns + source.
    `ingested_at` is auto-refreshed via the column default on conflict.
    """
    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "as_of": r.as_of,
            "pe_ratio": r.pe_ratio,
            "pb_ratio": r.pb_ratio,
            "dividend_yield": r.dividend_yield,
            "eps": r.eps,
            "revenue": r.revenue,
            "payload": r.payload,
            "source": r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=FundamentalsSnapshot,
        payload=payload,
        index_elements=["market", "symbol", "as_of"],
        update_cols=(
            "pe_ratio", "pb_ratio", "dividend_yield", "eps", "revenue",
            "payload", "source",
        ),
    )


async def read_latest_fundamentals(
    db: AsyncSession, market: str, symbol: str, *, max_age_days: int,
) -> dict[str, Any] | None:
    """Return the most recent snapshot if newer than `max_age_days`.

    Output shape mirrors `tw_market_service.get_fundamentals` (PE / PB /
    dividend_yield + symbol/market/exchange) so the caller can return it
    almost verbatim.
    """
    cutoff = date.today() - timedelta(days=max_age_days)
    stmt = (
        select(FundamentalsSnapshot)
        .where(
            FundamentalsSnapshot.market == market,
            FundamentalsSnapshot.symbol == symbol,
            FundamentalsSnapshot.as_of >= cutoff,
        )
        .order_by(FundamentalsSnapshot.as_of.desc())
        .limit(1)
    )
    row = await db.scalar(stmt)
    if row is None:
        return None
    return {
        "symbol":         row.symbol,
        "market":         row.market,
        "pe_ratio":       float(row.pe_ratio) if row.pe_ratio is not None else None,
        "pb_ratio":       float(row.pb_ratio) if row.pb_ratio is not None else None,
        "dividend_yield": float(row.dividend_yield) if row.dividend_yield is not None else None,
        "eps":            float(row.eps) if row.eps is not None else None,
        "revenue":        float(row.revenue) if row.revenue is not None else None,
        "as_of":          row.as_of.isoformat(),
        "data_source":    row.source,
    }


async def read_fundamentals_as_of(
    db: AsyncSession, market: str, symbol: str, *, as_of: date,
) -> dict[str, Any] | None:
    """Most recent snapshot at or before `as_of` — the backtest twin of
    `read_latest_fundamentals`, which anchors its staleness window on
    `date.today()` and so can only answer "now".

    Safe to read historically because the rows are point-in-time by
    construction: `backfill_fundamentals_history` sources valuations
    from TWSE's dated `BWIBBU_d` report and statement fields from the
    quarters that had closed on/before each target day. Nothing
    recomputes a past row against later data — unlike
    `tw_revenue_monthly.revenue_yoy`, which a later backfill can
    restate and which `read_top_revenue_growers` therefore masks in
    backtest mode.

    No staleness cap: a replay anchored on a day the ingest job hadn't
    yet covered should fall back to the closest earlier snapshot rather
    than show nothing, and `as_of` on the returned dict makes the age
    visible to the caller.
    """
    stmt = (
        select(FundamentalsSnapshot)
        .where(
            FundamentalsSnapshot.market == market,
            FundamentalsSnapshot.symbol == symbol,
            FundamentalsSnapshot.as_of <= as_of,
        )
        .order_by(FundamentalsSnapshot.as_of.desc())
        .limit(1)
    )
    row = await db.scalar(stmt)
    if row is None:
        return None
    return {
        "symbol":         row.symbol,
        "market":         row.market,
        "pe_ratio":       float(row.pe_ratio) if row.pe_ratio is not None else None,
        "pb_ratio":       float(row.pb_ratio) if row.pb_ratio is not None else None,
        "dividend_yield": float(row.dividend_yield) if row.dividend_yield is not None else None,
        "eps":            float(row.eps) if row.eps is not None else None,
        "revenue":        float(row.revenue) if row.revenue is not None else None,
        "as_of":          row.as_of.isoformat(),
        "data_source":    row.source,
    }


async def read_fundamentals_as_of_autosession(
    market: str, symbol: str, *, as_of: date,
) -> dict[str, Any] | None:
    """Session-owning wrapper — focus briefs are fanned out alongside
    tasks that share the caller's session, so they open their own."""
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        return await read_fundamentals_as_of(db, market, symbol, as_of=as_of)


async def upsert_fundamentals_snapshots_autosession(
    rows: Iterable[FundamentalsSnapshotRow],
) -> int:
    """Open a fresh session and upsert. Errors are caught + logged so
    a Postgres outage in the read-path write-back never breaks the
    request."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await upsert_fundamentals_snapshots(db, rows)
    except Exception as exc:
        log.warning("ingest.fundamentals.write_error",
                    extra={"market": rows[0].market, "count": len(rows), "error": str(exc)})
        return 0


async def read_latest_fundamentals_autosession(
    market: str, symbol: str, *, max_age_days: int,
) -> dict[str, Any] | None:
    """Same as `read_latest_fundamentals` but opens its own session and
    swallows DB errors so the read path falls through to upstream
    cleanly when Postgres is unhealthy."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_latest_fundamentals(
                db, market, symbol, max_age_days=max_age_days,
            )
    except Exception as exc:
        log.warning("ingest.fundamentals.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return None
