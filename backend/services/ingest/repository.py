"""Central repository for the scheduled-fetch subsystem.

Phase 1 covers OHLCV reads/writes plus a Redis-backed health snapshot
the admin UI can poll. Schedulers write here; `tw_market_service` reads
here as the new tier between Redis and the upstream waterfall.
"""
import hashlib
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_ as sa_or, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import cache_delete, cache_get, cache_incr, cache_set, get_redis
from db.session import AsyncSessionLocal
from models.fundamentals_snapshot import FundamentalsSnapshot
from models.news_article import NewsArticle
from models.ohlcv_daily import OhlcvDaily
from models.quote_snapshot import QuoteSnapshot
from models.tw_chip_metrics import TwInstitutionalDaily, TwMarginDaily
from models.tw_stock_futures_oi import TwStockFuturesOi
from models.tw_govt_bank_flow import TwGovtBankFlowDaily
from models.tw_holdings_aggregates import (
    TwMarketInstitutionalDaily,
    TwStockShareholding,
)
from models.tw_revenue_monthly import TwRevenueMonthly
from models.tw_risk_signals import (
    TwStockDayTradingDaily,
    TwStockDisposition,
    TwStockSuspended,
)
from models.tw_stock_buyback import TwStockBuyback

log = logging.getLogger(__name__)

# Redis key for per-job health snapshots, scanned by the admin endpoint.
_HEALTH_KEY_PREFIX = "ingest:health:"
_HEALTH_TTL = 7 * 24 * 3600   # 7 days; cron runs at most weekly


# ── OHLCV ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OhlcvBar:
    market: str
    symbol: str
    ts: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    source: str

    @classmethod
    def from_connector_row(
        cls, market: str, symbol: str, source: str, row: dict[str, Any],
    ) -> "OhlcvBar | None":
        """Coerce a connector dict (`{time, open, high, low, close, volume}`)
        into a typed OhlcvBar. Returns None for malformed rows so callers
        can `filter(None, ...)` instead of try/except per row.

        Drops rows with non-positive ``close`` or ``open`` — listed
        equities never trade at 0 and an upstream zero is almost
        always a parser swallowing a placeholder ('—', 'N/A'). Logs
        each dropped row at WARNING so operators can see how many
        junk rows the upstream is emitting today.
        """
        ts_raw = row.get("time") or row.get("date")
        if not ts_raw:
            return None
        try:
            ts = date.fromisoformat(str(ts_raw)[:10])
        except ValueError:
            return None
        close = _to_float(row.get("close"))
        open_ = _to_float(row.get("open"))
        if close is not None and close <= 0:
            log.warning(
                "ohlcv.non_positive_close",
                extra={
                    "market": market, "symbol": symbol, "source": source,
                    "ts": ts.isoformat(), "close": close,
                },
            )
            return None
        if open_ is not None and open_ <= 0:
            log.warning(
                "ohlcv.non_positive_open",
                extra={
                    "market": market, "symbol": symbol, "source": source,
                    "ts": ts.isoformat(), "open": open_,
                },
            )
            return None
        return cls(
            market=market,
            symbol=symbol,
            ts=ts,
            open=open_,
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=close,
            volume=_to_int(row.get("volume")),
            source=source,
        )


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# asyncpg's wire protocol caps a single statement at 32767 bind parameters.
# Market-wide ingests (e.g. 八大行庫 ~13K rows × 6 cols, 股權分散 ~35K rows × 9
# cols) blow past that in one shot and surface as InterfaceError. `_chunked_upsert`
# batches the payload so any market-wide bulk insert stays under the wire cap
# without callers having to think about it.
_PG_PARAM_LIMIT = 32000  # leave headroom under the 32767 hard cap


async def _chunked_upsert(
    db: AsyncSession,
    *,
    model: type,
    payload: list[dict[str, Any]],
    index_elements: list[str],
    update_cols: tuple[str, ...],
) -> int:
    """Dialect-aware ON CONFLICT upsert chunked under asyncpg's bind-param cap.

    Single chunk for small payloads (behaviourally identical to the previous
    one-shot insert); split for large ones.
    """
    if not payload:
        return 0
    cols = len(payload[0])
    chunk_size = max(1, _PG_PARAM_LIMIT // cols)
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert
    for i in range(0, len(payload), chunk_size):
        batch = payload[i:i + chunk_size]
        stmt = insert_fn(model).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={k: getattr(stmt.excluded, k) for k in update_cols},
        )
        await db.execute(stmt)
    await db.commit()
    return len(payload)


def _bar_to_row(bar: OhlcvBar) -> dict[str, Any]:
    return {
        "market": bar.market,
        "symbol": bar.symbol,
        "ts": bar.ts,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "source": bar.source,
    }


async def upsert_ohlcv_bars(db: AsyncSession, bars: Iterable[OhlcvBar]) -> int:
    """Bulk upsert OHLCV bars. Returns number of rows passed in.

    Uses dialect-specific ON CONFLICT for Postgres + SQLite (the only two
    dialects this project runs on). Bars with the same (market, symbol, ts)
    are last-write-wins on the price columns + source. `ingested_at` is
    refreshed on conflict so operators can tell when a bar was last
    re-ingested.
    """
    payload = [_bar_to_row(b) for b in bars]
    return await _chunked_upsert(
        db,
        model=OhlcvDaily,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=("open", "high", "low", "close", "volume", "source"),
    )


async def read_ohlcv_range(
    db: AsyncSession,
    market: str,
    symbol: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Return bars in `[start, end]` (inclusive) ordered ascending by ts.

    Output shape matches the connector layer (`time` ISO string, numeric
    OHLCV) so it's a drop-in for `tw_market_service.get_history`.
    """
    stmt = (
        select(OhlcvDaily)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.symbol == symbol,
            OhlcvDaily.ts >= start,
            OhlcvDaily.ts <= end,
        )
        .order_by(OhlcvDaily.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "time":   r.ts.isoformat(),
            "open":   float(r.open) if r.open is not None else None,
            "high":   float(r.high) if r.high is not None else None,
            "low":    float(r.low) if r.low is not None else None,
            "close":  float(r.close) if r.close is not None else None,
            "volume": int(r.volume) if r.volume is not None else 0,
        }
        for r in rows
    ]


# ── TW chip metrics (法人 / 融資融券) ──────────────────────────────


@dataclass(frozen=True)
class InstitutionalDailyRow:
    """法人買賣超 — fini / sitc / dealer buy + sell volumes."""
    market: str
    symbol: str
    ts: date
    fini_buy: int | None
    fini_sell: int | None
    sitc_buy: int | None
    sitc_sell: int | None
    dealer_buy: int | None
    dealer_sell: int | None
    source: str


@dataclass(frozen=True)
class MarginDailyRow:
    """融資融券 — daily margin / short purchase + balance."""
    market: str
    symbol: str
    ts: date
    margin_purchase: int | None
    margin_balance: int | None
    short_sale: int | None
    short_balance: int | None
    source: str


def _row_to_dict(row: Any, *, fields: tuple[str, ...]) -> dict[str, Any]:
    """Coerce a dataclass / ORM row to a dict for bulk-insert."""
    return {f: getattr(row, f) for f in fields}


_INSTITUTIONAL_FIELDS = (
    "market", "symbol", "ts",
    "fini_buy", "fini_sell",
    "sitc_buy", "sitc_sell",
    "dealer_buy", "dealer_sell",
    "source",
)


_MARGIN_FIELDS = (
    "market", "symbol", "ts",
    "margin_purchase", "margin_balance",
    "short_sale", "short_balance",
    "source",
)


async def upsert_institutional_daily(
    db: AsyncSession, rows: Iterable[InstitutionalDailyRow],
) -> int:
    """Bulk upsert. ON CONFLICT (market, symbol, ts) updates the
    metric columns + source so a re-ingest on the same day overwrites
    stale values (e.g. when TWSE corrects a bar)."""
    payload = [_row_to_dict(r, fields=_INSTITUTIONAL_FIELDS) for r in rows]
    return await _chunked_upsert(
        db,
        model=TwInstitutionalDaily,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=(
            "fini_buy", "fini_sell",
            "sitc_buy", "sitc_sell",
            "dealer_buy", "dealer_sell",
            "source",
        ),
    )


async def upsert_margin_daily(
    db: AsyncSession, rows: Iterable[MarginDailyRow],
) -> int:
    payload = [_row_to_dict(r, fields=_MARGIN_FIELDS) for r in rows]
    return await _chunked_upsert(
        db,
        model=TwMarginDaily,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=(
            "margin_purchase", "margin_balance",
            "short_sale", "short_balance",
            "source",
        ),
    )


def _institutional_row_out(r: TwInstitutionalDaily) -> dict[str, Any]:
    """Output shape mirrors the FinMind connector's per-symbol row so
    `tw_market_service.get_institutional`'s callers don't care which
    tier served them."""
    return {
        "date":        r.ts.isoformat(),
        "symbol":      r.symbol,
        "fini_buy":    int(r.fini_buy) if r.fini_buy is not None else 0,
        "fini_sell":   int(r.fini_sell) if r.fini_sell is not None else 0,
        "sitc_buy":    int(r.sitc_buy) if r.sitc_buy is not None else 0,
        "sitc_sell":   int(r.sitc_sell) if r.sitc_sell is not None else 0,
        "dealer_buy":  int(r.dealer_buy) if r.dealer_buy is not None else 0,
        "dealer_sell": int(r.dealer_sell) if r.dealer_sell is not None else 0,
    }


def _margin_row_out(r: TwMarginDaily) -> dict[str, Any]:
    return {
        "date":            r.ts.isoformat(),
        "symbol":          r.symbol,
        "margin_purchase": int(r.margin_purchase) if r.margin_purchase is not None else 0,
        "margin_balance":  int(r.margin_balance) if r.margin_balance is not None else 0,
        "short_sale":      int(r.short_sale) if r.short_sale is not None else 0,
        "short_balance":   int(r.short_balance) if r.short_balance is not None else 0,
    }


async def read_institutional_range(
    db: AsyncSession, market: str, symbol: str, start: date, end: date,
) -> list[dict[str, Any]]:
    """Per-symbol date range, ordered ascending. Output shape mirrors
    `data/tw/finmind_connector.get_institutional` so the read-tier
    plumbs straight into `tw_market_service.get_institutional`."""
    stmt = (
        select(TwInstitutionalDaily)
        .where(
            TwInstitutionalDaily.market == market,
            TwInstitutionalDaily.symbol == symbol,
            TwInstitutionalDaily.ts >= start,
            TwInstitutionalDaily.ts <= end,
        )
        .order_by(TwInstitutionalDaily.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [_institutional_row_out(r) for r in rows]


async def read_margin_range(
    db: AsyncSession, market: str, symbol: str, start: date, end: date,
) -> list[dict[str, Any]]:
    stmt = (
        select(TwMarginDaily)
        .where(
            TwMarginDaily.market == market,
            TwMarginDaily.symbol == symbol,
            TwMarginDaily.ts >= start,
            TwMarginDaily.ts <= end,
        )
        .order_by(TwMarginDaily.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [_margin_row_out(r) for r in rows]


async def read_top_foreign_buyers(
    db: AsyncSession,
    market: str = "TW",
    *,
    days: int = 5,
    limit: int = 10,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Aggregate net foreign buy (`fini_buy - fini_sell`) over the
    last `days` trading dates ending at `as_of` (default: today) and
    return the top `limit` symbols.

    Used by `discussion_service.gather_market_context` so personas
    can reference "外資連 5 日買超 N 億" without burning an LLM tool
    call on the raw rows. Returns an empty list when the table hasn't
    been populated yet (fresh deploy, ingest task hasn't run yet).
    """
    end = as_of or date.today()
    start = end - timedelta(days=days * 2)  # widen for weekend / holiday
    stmt = (
        select(TwInstitutionalDaily)
        .where(
            TwInstitutionalDaily.market == market,
            TwInstitutionalDaily.ts >= start,
            TwInstitutionalDaily.ts <= end,
        )
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        return []
    by_symbol: dict[str, int] = {}
    for r in rows:
        net = (int(r.fini_buy or 0) - int(r.fini_sell or 0))
        by_symbol[r.symbol] = by_symbol.get(r.symbol, 0) + net
    ordered = sorted(by_symbol.items(), key=lambda kv: kv[1], reverse=True)
    return [{"symbol": sym, "net_foreign_buy": net} for sym, net in ordered[:limit]]


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


# ── 八大行庫 (TW government bank daily flow) ────────────────────

@dataclass(frozen=True)
class GovtBankFlowRow:
    """One bank's daily buy/sell aggregate."""
    market: str
    ts: date
    bank_name: str
    buy_amount: int | None
    sell_amount: int | None
    source: str


async def upsert_govt_bank_flows(
    db: AsyncSession, rows: Iterable[GovtBankFlowRow],
) -> int:
    """Bulk upsert keyed on (market, ts, bank_name)."""
    payload = [
        {
            "market":      r.market,
            "ts":          r.ts,
            "bank_name":   r.bank_name,
            "buy_amount":  r.buy_amount,
            "sell_amount": r.sell_amount,
            "source":      r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwGovtBankFlowDaily,
        payload=payload,
        index_elements=["market", "ts", "bank_name"],
        update_cols=("buy_amount", "sell_amount", "source"),
    )


async def read_recent_govt_bank_flow(
    db: AsyncSession, *, market: str = "TW", days: int = 5,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Last `days` of trading-day aggregates ending at `as_of` (default:
    today), summed across the eight banks. Used by the discussion
    context block — personas care about the headline net flow ("八大行庫
    +12 億"), not the per-bank breakdown.

    Returns one row per date, newest first:

        [{"date": "2026-04-30", "buy_total": 8_500_000_000,
          "sell_total": 6_300_000_000, "net": 2_200_000_000}, …]

    Empty list when the cron hasn't populated yet — caller
    interprets as "no signal".
    """
    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 2)  # +slack for weekends
    stmt = (
        select(TwGovtBankFlowDaily)
        .where(
            TwGovtBankFlowDaily.market == market,
            TwGovtBankFlowDaily.ts >= cutoff,
            TwGovtBankFlowDaily.ts <= end,
        )
        .order_by(TwGovtBankFlowDaily.ts.desc())
    )
    rows = (await db.scalars(stmt)).all()
    by_date: dict[date, dict[str, int]] = {}
    for r in rows:
        d = by_date.setdefault(r.ts, {"buy": 0, "sell": 0})
        d["buy"] += int(r.buy_amount or 0)
        d["sell"] += int(r.sell_amount or 0)
    out: list[dict[str, Any]] = []
    for ts in sorted(by_date.keys(), reverse=True)[:days]:
        b = by_date[ts]
        out.append({
            "date":       ts.isoformat(),
            "buy_total":  b["buy"],
            "sell_total": b["sell"],
            "net":        b["buy"] - b["sell"],
        })
    return out


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


# ── 股權分散 (TwStockShareholding) ────────────────────────────────

@dataclass(frozen=True)
class ShareholdingRow:
    market: str
    symbol: str
    ts: date
    bucket_id: int
    bucket_label: str | None
    holders_count: int | None
    shares_count: int | None
    shares_percent: float | None
    source: str


async def upsert_shareholdings(
    db: AsyncSession, rows: Iterable[ShareholdingRow],
) -> int:
    payload = [
        {
            "market":         r.market,
            "symbol":         r.symbol,
            "ts":             r.ts,
            "bucket_id":      r.bucket_id,
            "bucket_label":   r.bucket_label,
            "holders_count":  r.holders_count,
            "shares_count":   r.shares_count,
            "shares_percent": r.shares_percent,
            "source":         r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockShareholding,
        payload=payload,
        index_elements=["market", "symbol", "ts", "bucket_id"],
        update_cols=(
            "bucket_label", "holders_count", "shares_count",
            "shares_percent", "source",
        ),
    )


async def read_latest_shareholding(
    db: AsyncSession, *, market: str, symbol: str,
) -> list[dict[str, Any]]:
    """All buckets for the most recent publication date of `symbol`.
    Used by the StockDetailPage shareholder-distribution card and
    the discussion-context aggregator.
    """
    latest_stmt = (
        select(TwStockShareholding.ts)
        .where(
            TwStockShareholding.market == market,
            TwStockShareholding.symbol == symbol,
        )
        .order_by(TwStockShareholding.ts.desc())
        .limit(1)
    )
    latest_ts = (await db.scalars(latest_stmt)).first()
    if latest_ts is None:
        return []
    stmt = (
        select(TwStockShareholding)
        .where(
            TwStockShareholding.market == market,
            TwStockShareholding.symbol == symbol,
            TwStockShareholding.ts == latest_ts,
        )
        .order_by(TwStockShareholding.bucket_id.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "ts":             r.ts.isoformat(),
            "bucket_id":      r.bucket_id,
            "bucket_label":   r.bucket_label,
            "holders_count":  int(r.holders_count) if r.holders_count is not None else None,
            "shares_count":   int(r.shares_count) if r.shares_count is not None else None,
            "shares_percent": float(r.shares_percent) if r.shares_percent is not None else None,
        }
        for r in rows
    ]


# Top buckets we sum for the "concentration" metric. FinMind's
# TaiwanStockShareholding response uses bucket_id 1-15, ascending
# by holding-size (1 = retail < 1 lot, 15 = > 1,000 lots which is
# the canonical 千張大戶 threshold). Top 3 buckets capture
# institutional + 千張大戶 concentration. Operator-tunable in
# future via runtime config if the bucket scheme drifts.
_HOLDINGS_TOP_BUCKETS = (13, 14, 15)
# Trend band as a percentage-point change (NOT a fraction). +1 pp
# in concentration over a month is a meaningful institutional move;
# < 1 pp is noise.
_HOLDINGS_TREND_PP_BAND = 1.0


async def read_holdings_concentration_trend(
    db: AsyncSession, *, market: str, symbol: str,
    weeks: int = 4, as_of: date | None = None,
) -> dict[str, Any] | None:
    """Per-symbol large-holder concentration trend over the last
    `weeks` weekly publications.

    "Concentration" = sum of `shares_percent` for the top-3 holder
    buckets (bucket_id 13/14/15 = institutional + 千張大戶 ≥ 1,000
    lots). Tracking its trend tells us:

        latest > earlier + 1 pp  →  rising  (institutional accumulation)
        latest < earlier - 1 pp  →  falling (institutional distribution)
        within ±1 pp             →  stable

    Returns None when:
      - fewer than 2 weekly publications in the window
        (can't compute a trend with one snapshot), OR
      - the symbol's archive is empty for the window.

    Distinct from `read_latest_shareholding` which returns the full
    bucket breakdown for a single date — operators viewing the
    StockDetailPage want every bucket; personas in a discussion
    only need the directional read. Two readers, two consumers.
    """
    end = as_of or date.today()
    cutoff = end - timedelta(days=weeks * 7)
    stmt = (
        select(TwStockShareholding)
        .where(
            TwStockShareholding.market == market,
            TwStockShareholding.symbol == symbol,
            TwStockShareholding.bucket_id.in_(_HOLDINGS_TOP_BUCKETS),
            TwStockShareholding.ts >= cutoff,
            TwStockShareholding.ts <= end,
        )
        .order_by(TwStockShareholding.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        return None

    # Sum top-bucket shares_percent per date.
    by_ts: dict[date, float] = {}
    for r in rows:
        if r.shares_percent is None:
            continue
        by_ts[r.ts] = by_ts.get(r.ts, 0.0) + float(r.shares_percent)
    if len(by_ts) < 2:
        return None

    sorted_ts = sorted(by_ts.keys())
    earliest_ts = sorted_ts[0]
    latest_ts = sorted_ts[-1]
    latest_pct = by_ts[latest_ts]
    earliest_pct = by_ts[earliest_ts]
    change_pp = latest_pct - earliest_pct
    if change_pp > _HOLDINGS_TREND_PP_BAND:
        trend = "rising"
    elif change_pp < -_HOLDINGS_TREND_PP_BAND:
        trend = "falling"
    else:
        trend = "stable"
    return {
        "latest_date":            latest_ts.isoformat(),
        "publication_count":      len(by_ts),
        "latest_top_holders_pct": round(latest_pct, 2),
        "earliest_top_holders_pct": round(earliest_pct, 2),
        "change_pp":              round(change_pp, 2),
        "trend":                  trend,
    }


# ── 全市場三大法人日報 (TwMarketInstitutionalDaily) ─────────────

@dataclass(frozen=True)
class MarketInstitutionalRow:
    market: str
    ts: date
    foreign_buy: int | None
    foreign_sell: int | None
    sitc_buy: int | None
    sitc_sell: int | None
    dealer_buy: int | None
    dealer_sell: int | None
    source: str


async def upsert_market_institutional_daily(
    db: AsyncSession, rows: Iterable[MarketInstitutionalRow],
) -> int:
    payload = [
        {
            "market":       r.market,
            "ts":           r.ts,
            "foreign_buy":  r.foreign_buy,
            "foreign_sell": r.foreign_sell,
            "sitc_buy":     r.sitc_buy,
            "sitc_sell":    r.sitc_sell,
            "dealer_buy":   r.dealer_buy,
            "dealer_sell":  r.dealer_sell,
            "source":       r.source,
        }
        for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwMarketInstitutionalDaily,
        payload=payload,
        index_elements=["market", "ts"],
        update_cols=(
            "foreign_buy", "foreign_sell", "sitc_buy", "sitc_sell",
            "dealer_buy", "dealer_sell", "source",
        ),
    )


async def read_recent_market_institutional(
    db: AsyncSession, *, market: str = "TW", days: int = 5,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Last `days` of full-market 三大法人 aggregates ending at
    `as_of` (default: today), newest first. Used in the discussion
    context block as the index-level headline ("外資今日對台股淨買超
    +250 億 / 連 3 日買超")."""
    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 3)  # weekend slack
    stmt = (
        select(TwMarketInstitutionalDaily)
        .where(
            TwMarketInstitutionalDaily.market == market,
            TwMarketInstitutionalDaily.ts >= cutoff,
            TwMarketInstitutionalDaily.ts <= end,
        )
        .order_by(TwMarketInstitutionalDaily.ts.desc())
        .limit(days)
    )
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "date":         r.ts.isoformat(),
            "foreign_net":  (int(r.foreign_buy or 0) - int(r.foreign_sell or 0)),
            "sitc_net":     (int(r.sitc_buy or 0) - int(r.sitc_sell or 0)),
            "dealer_net":   (int(r.dealer_buy or 0) - int(r.dealer_sell or 0)),
            "total_net":    (
                int(r.foreign_buy or 0) - int(r.foreign_sell or 0)
                + int(r.sitc_buy or 0) - int(r.sitc_sell or 0)
                + int(r.dealer_buy or 0) - int(r.dealer_sell or 0)
            ),
        }
        for r in rows
    ]


# ── Quote snapshots ────────────────────────────────────────────────

@dataclass(frozen=True)
class QuoteSnapshotRow:
    market: str
    symbol: str
    ts: datetime
    last_price: float | None
    change_pct: float | None
    prev_close: float | None
    volume: int | None
    source: str


def _utc_timestamp(value: datetime) -> float:
    """SQLite drops tzinfo, but quote snapshot timestamps are stored as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.timestamp()


async def insert_quote_snapshot(db: AsyncSession, snap: QuoteSnapshotRow) -> None:
    """Insert one quote snapshot. Same (market, symbol, ts) is a no-op
    instead of an error — the refresh task runs every 60 s and we'd
    rather drop a duplicate than crash if two pods race the lock."""
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    row = {
        "market": snap.market,
        "symbol": snap.symbol,
        "ts": snap.ts,
        "last_price": snap.last_price,
        "change_pct": snap.change_pct,
        "prev_close": snap.prev_close,
        "volume": snap.volume,
        "source": snap.source,
    }
    if dialect == "sqlite":
        stmt = sqlite_insert(QuoteSnapshot).values(row).on_conflict_do_nothing(
            index_elements=["market", "symbol", "ts"],
        )
    else:
        stmt = pg_insert(QuoteSnapshot).values(row).on_conflict_do_nothing(
            index_elements=["market", "symbol", "ts"],
        )
    await db.execute(stmt)
    await db.commit()


async def read_latest_quote(
    db: AsyncSession, market: str, symbol: str, *, max_age_seconds: int,
) -> dict[str, Any] | None:
    """Return the most recent snapshot for (market, symbol) if it's within
    `max_age_seconds` of `now`. Returns None if no row found or stale.

    Output shape matches `tw_market_service._normalize_quote` so the
    caller can drop it straight into the response after attaching `tz`
    and `is_market_open`.
    """
    cutoff = datetime.now(UTC).timestamp() - max_age_seconds
    stmt = (
        select(QuoteSnapshot)
        .where(QuoteSnapshot.market == market, QuoteSnapshot.symbol == symbol)
        .order_by(QuoteSnapshot.ts.desc())
        .limit(1)
    )
    row = await db.scalar(stmt)
    if row is None or _utc_timestamp(row.ts) < cutoff:
        return None
    return {
        "symbol":      row.symbol,
        "market":      row.market,
        "price":       float(row.last_price) if row.last_price is not None else 0,
        "change_pct":  float(row.change_pct) if row.change_pct is not None else None,
        "prev_close":  float(row.prev_close) if row.prev_close is not None else None,
        "volume":      int(row.volume) if row.volume is not None else 0,
        "ts":          int(_utc_timestamp(row.ts) * 1000),
        "data_source": row.source,
    }


async def prune_quote_snapshots(db: AsyncSession, *, older_than_days: int) -> int:
    """Delete snapshots older than `older_than_days`. Returns deleted count."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    stmt = delete(QuoteSnapshot).where(QuoteSnapshot.ts < cutoff)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def insert_quote_snapshot_autosession(snap: QuoteSnapshotRow) -> None:
    """Same as `insert_quote_snapshot` but opens its own session.

    Used by the TW refresh task which doesn't already have a session in
    scope. DB errors are swallowed because the snapshot is best-effort —
    the live WS push must not be blocked on a Postgres outage.
    """
    try:
        async with AsyncSessionLocal() as db:
            await insert_quote_snapshot(db, snap)
    except Exception as exc:
        log.warning("ingest.quote_snapshot.write_error",
                    extra={"market": snap.market, "symbol": snap.symbol, "error": str(exc)})


async def read_latest_quote_autosession(
    market: str, symbol: str, *, max_age_seconds: int,
) -> dict[str, Any] | None:
    """Same as `read_latest_quote` but opens its own session and
    swallows DB errors so the read path falls through cleanly to upstream."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_latest_quote(
                db, market, symbol, max_age_seconds=max_age_seconds,
            )
    except Exception as exc:
        log.warning("ingest.quote_snapshot.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return None


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


# ── News articles ──────────────────────────────────────────────────

# Strip whitespace + punctuation noise so the same article republished
# with trailing dots / smart quotes still hashes identically.
_NOISE_RE = re.compile(r"[\s　\.,;:!?。，！？“”‘’]+")


def _normalize_title(title: str) -> str:
    return _NOISE_RE.sub("", (title or "").lower())


def _canonical_link(link: str) -> str:
    """Strip query-string tracking params (utm_*, ref=...) so the same
    article shared via different campaigns deduplicates correctly."""
    if "?" not in link:
        return link.strip()
    base, _, qs = link.partition("?")
    keep = [
        kv for kv in qs.split("&")
        if kv and not kv.split("=", 1)[0].lower().startswith(("utm_", "ref", "fbclid", "gclid"))
    ]
    return base + (("?" + "&".join(keep)) if keep else "")


def compute_dedup_hash(title: str, link: str) -> str:
    raw = _normalize_title(title) + "|" + _canonical_link(link)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewsArticleRow:
    market: str
    symbol: str | None
    published_at: datetime
    title: str
    link: str
    publisher: str | None
    summary: str | None
    payload: dict[str, Any] | None
    source: str

    @property
    def dedup_hash(self) -> str:
        return compute_dedup_hash(self.title, self.link)


async def insert_news_articles(
    db: AsyncSession, rows: Iterable[NewsArticleRow],
) -> int:
    """Bulk insert with on-conflict-do-nothing on `dedup_hash`. Returns
    the number of input rows passed (not the number actually inserted —
    duplicates are silently dropped at the DB layer).

    Chunks the INSERT into batches of `_INSERT_NEWS_CHUNK_ROWS` so we
    don't blow past PostgreSQL's wire-protocol limit of 32767 query
    parameters per statement (`asyncpg.InterfaceError`). NewsArticleRow
    has 10 fields so 2 000 rows = 20 000 params, comfortably below the
    cap with headroom for future field additions. A 30-day backtest
    backfill of a busy news symbol can easily yield 5 000+ rows
    (~250/day × 31 days), which previously aborted the entire batch
    with zero rows persisted.
    """
    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "published_at": r.published_at,
            "title": r.title,
            "link": r.link,
            "publisher": r.publisher,
            "summary": r.summary,
            "payload": r.payload,
            "source": r.source,
            "dedup_hash": r.dedup_hash,
        }
        for r in rows
        if r.title and r.link
    ]
    if not payload:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert

    for i in range(0, len(payload), _INSERT_NEWS_CHUNK_ROWS):
        chunk = payload[i:i + _INSERT_NEWS_CHUNK_ROWS]
        stmt = insert_fn(NewsArticle).values(chunk).on_conflict_do_nothing(
            index_elements=["dedup_hash"],
        )
        await db.execute(stmt)
    await db.commit()
    return len(payload)


# PostgreSQL's wire protocol caps a single statement at 32767 query
# parameters. NewsArticleRow has 10 fields → 2 000 rows = 20 000
# params, leaving headroom. A FinMind day-by-day backfill over a
# busy 31-day window can return 5 000+ articles for one popular
# symbol, which used to crash the whole batch with
# `asyncpg.InterfaceError`.
_INSERT_NEWS_CHUNK_ROWS = 2_000


async def read_recent_news(
    db: AsyncSession,
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 20,
    max_age_days: int = 30,
    include_sentiment: bool = False,
) -> list[dict[str, Any]]:
    """Return the most recent articles for `(market, symbol)` newer than
    `max_age_days`. `symbol=None` matches market-wide articles
    (NULL symbol) only; pass an explicit symbol to fetch per-symbol news.

    Output shape mirrors `tw_market_service.get_news` so the caller can
    return the list verbatim. With `include_sentiment=True` each row
    additionally carries `sentiment_score` (-1..+1 or None) and
    `sentiment_label` (`bullish` / `bearish` / `neutral` or None) — used
    by the dashboard's RecentTWNews card to render colored badges
    without an extra round-trip.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(NewsArticle)
        .where(NewsArticle.market == market, NewsArticle.published_at >= cutoff)
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    if symbol is None:
        stmt = stmt.where(NewsArticle.symbol.is_(None))
    else:
        stmt = stmt.where(NewsArticle.symbol == symbol)
    rows = (await db.scalars(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {
            "title":        r.title,
            "publisher":    r.publisher or "",
            "link":         r.link,
            "published_at": r.published_at.isoformat(),
            "thumbnail":    (r.payload or {}).get("thumbnail") if r.payload else None,
            "data_source":  r.source,
        }
        if include_sentiment:
            item["sentiment_score"] = r.sentiment_score
            item["sentiment_label"] = r.sentiment_label
        out.append(item)
    return out


async def insert_news_articles_autosession(rows: Iterable[NewsArticleRow]) -> int:
    """Open own session + insert. Errors logged + swallowed."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await insert_news_articles(db, rows)
    except Exception as exc:
        log.warning("ingest.news.write_error",
                    extra={"market": rows[0].market, "count": len(rows), "error": str(exc)})
        return 0


async def read_recent_news_autosession(
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 20,
    max_age_days: int = 30,
    include_sentiment: bool = False,
) -> list[dict[str, Any]]:
    """Open own session + read. Errors logged; returns [] so the read
    path falls through cleanly to upstream."""
    try:
        async with AsyncSessionLocal() as db:
            return await read_recent_news(
                db, market,
                symbol=symbol, limit=limit, max_age_days=max_age_days,
                include_sentiment=include_sentiment,
            )
    except Exception as exc:
        log.warning("ingest.news.read_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return []


# ── Corporate announcements (TW MOPS 重大訊息, PR-D1) ───────────────


@dataclass(frozen=True)
class CorporateAnnouncementRow:
    """Canonical insert shape for `corporate_announcements`. Mirrors
    `NewsArticleRow` everywhere it makes sense (sentiment fields are
    populated downstream by the same scorer); the meaningful
    differences are `category` (required) and `body` (separate from
    title because MOPS' multi-paragraph disclosures benefit from
    being quoted verbatim into the discussion ctx)."""
    market: str
    symbol: str
    announced_at: datetime
    category: str
    title: str
    body: str | None
    source_url: str | None
    source: str
    dedup_hash: str


async def insert_corporate_announcements(
    db: AsyncSession,
    rows: Iterable[CorporateAnnouncementRow],
) -> int:
    """Bulk insert with on-conflict-do-nothing on `dedup_hash`. Same
    chunking strategy as `insert_news_articles` (PG wire-protocol
    32 767 param cap; 11 fields per row → 2 000 rows = 22 000 params,
    headroom-comfortable)."""
    from models.corporate_announcement import CorporateAnnouncement

    payload = [
        {
            "market": r.market,
            "symbol": r.symbol,
            "announced_at": r.announced_at,
            "category": r.category,
            "title": r.title,
            "body": r.body,
            "source_url": r.source_url,
            "source": r.source,
            "dedup_hash": r.dedup_hash,
        }
        for r in rows
        if r.title and r.symbol
    ]
    if not payload:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert

    for i in range(0, len(payload), _INSERT_NEWS_CHUNK_ROWS):
        chunk = payload[i:i + _INSERT_NEWS_CHUNK_ROWS]
        stmt = insert_fn(CorporateAnnouncement).values(chunk).on_conflict_do_nothing(
            index_elements=["dedup_hash"],
        )
        await db.execute(stmt)
    await db.commit()
    return len(payload)


async def insert_corporate_announcements_autosession(
    rows: Iterable[CorporateAnnouncementRow],
) -> int:
    rows = list(rows)
    if not rows:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await insert_corporate_announcements(db, rows)
    except Exception as exc:
        log.warning(
            "ingest.announcements.write_error",
            extra={
                "market": rows[0].market, "count": len(rows),
                "error": str(exc),
            },
        )
        return 0


async def read_recent_announcements(
    db: AsyncSession,
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 10,
    max_age_days: int = 7,
) -> list[dict[str, Any]]:
    """Read the most recent material-info disclosures for
    `(market, symbol)` newer than `max_age_days`. Used by the
    discussion ctx block (PR-D4 wires it). Default window is 7
    days because MOPS material info loses signal value fast — the
    market has already priced it in within a session or two for
    most categories.

    Returns the rows as plain dicts so the ctx path can serialize
    cleanly without an ORM dependency. Sentiment columns are
    included so personas can see "this earnings disclosure scored
    +0.6 (bullish)" inline.
    """
    from models.corporate_announcement import CorporateAnnouncement

    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(CorporateAnnouncement)
        .where(
            CorporateAnnouncement.market == market,
            CorporateAnnouncement.announced_at >= cutoff,
        )
        .order_by(CorporateAnnouncement.announced_at.desc())
        .limit(limit)
    )
    if symbol is not None:
        stmt = stmt.where(CorporateAnnouncement.symbol == symbol)
    rows = (await db.scalars(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "symbol":              r.symbol,
            "announced_at":        r.announced_at.isoformat(),
            "category":            r.category,
            "title":               r.title,
            "body":                r.body,
            "source_url":          r.source_url,
            "sentiment_score":     r.sentiment_score,
            "sentiment_label":     r.sentiment_label,
        })
    return out


async def read_recent_announcements_autosession(
    market: str,
    *,
    symbol: str | None = None,
    limit: int = 10,
    max_age_days: int = 7,
) -> list[dict[str, Any]]:
    try:
        async with AsyncSessionLocal() as db:
            return await read_recent_announcements(
                db, market,
                symbol=symbol, limit=limit, max_age_days=max_age_days,
            )
    except Exception as exc:
        log.warning(
            "ingest.announcements.read_error",
            extra={"market": market, "symbol": symbol, "error": str(exc)},
        )
        return []


# ── Health snapshot ────────────────────────────────────────────────

@dataclass
class IngestHealth:
    job_id: str
    last_run_at: str | None
    ok: bool
    row_count: int
    error: str | None
    silent_deny: str | None = None
    latest_data_ts: str | None = None


def _classify_outcome(
    *, ok: bool, error: str | None, silent_deny: str | None,
) -> str:
    """Map a record_health call to a stable Prometheus outcome label.

    silent_deny wins over plain failure so operators can split paywall /
    quota events from real upstream errors. `skipped` and `queued` are
    fold together — both mean "we deliberately didn't try this run".
    """
    if ok:
        return "ok"
    if silent_deny is not None:
        return "silent_deny"
    err = (error or "").lower()
    if err.startswith("skipped") or err.startswith("queued"):
        return "skipped"
    return "failed"


async def record_health(
    job_id: str,
    *,
    ok: bool,
    row_count: int = 0,
    error: str | None = None,
    silent_deny: str | None = None,
    latest_data_ts: "date | datetime | None" = None,
    source: str | None = None,
) -> None:
    """Persist a per-job health snapshot in Redis + emit Prometheus.

    Stored as a single JSON blob keyed by job_id with a long TTL so the
    admin dashboard can reflect "last successful run" even after a quiet
    weekend. A separate Postgres table would be more durable but isn't
    needed yet — Redis state is regenerated on the next scheduled run.

    The Prometheus side is wired here (rather than per-task) so every
    existing caller picks up `ingest_runs_total` / `ingest_rows_*` for
    free; new caller-side kwargs (silent_deny, latest_data_ts, source)
    are all optional + keyword-only so the ~50 existing call sites
    keep working unchanged.
    """
    ts_iso: str | None = None
    if latest_data_ts is not None:
        if isinstance(latest_data_ts, datetime):
            ts_iso = latest_data_ts.isoformat()
        else:
            ts_iso = latest_data_ts.isoformat()

    payload = json.dumps({
        "job_id": job_id,
        "last_run_at": datetime.now(UTC).isoformat(),
        "ok": ok,
        "row_count": int(row_count),
        "error": error,
        "silent_deny": silent_deny,
        "latest_data_ts": ts_iso,
    })
    try:
        await cache_set(_HEALTH_KEY_PREFIX + job_id, payload, _HEALTH_TTL)
    except Exception as exc:
        log.warning("ingest.health.record_failed",
                    extra={"job_id": job_id, "error": str(exc)})

    outcome = _classify_outcome(
        ok=ok, error=error, silent_deny=silent_deny,
    )

    # Append-only history for the AdminPage 7-day sparkline. Best-
    # effort: a DB write failure here logs but doesn't break the cron
    # — the Redis snapshot above is the source of truth for the
    # "current" status row, history is purely cumulative.
    try:
        from models.ingest_health_history import IngestHealthHistory
        async with AsyncSessionLocal() as db:
            db.add(IngestHealthHistory(
                job_id=job_id,
                outcome=outcome,
                row_count=int(row_count),
            ))
            await db.commit()
    except Exception as exc:
        log.debug("ingest.health.history_append_failed",
                  extra={"job_id": job_id, "error": str(exc)})

    try:
        from middleware.metrics import (
            INGEST_DATA_FRESHNESS_SECONDS,
            INGEST_ROWS_WRITTEN_TOTAL,
            INGEST_RUNS_TOTAL,
            INGEST_SILENT_DENY_TOTAL,
        )
        INGEST_RUNS_TOTAL.labels(job_id=job_id, outcome=outcome).inc()
        if ok and row_count > 0:
            INGEST_ROWS_WRITTEN_TOTAL.labels(job_id=job_id).inc(row_count)
        if silent_deny is not None:
            INGEST_SILENT_DENY_TOTAL.labels(
                job_id=job_id, source=(source or "unknown"),
            ).inc()
        if latest_data_ts is not None:
            now_utc = datetime.now(UTC)
            if isinstance(latest_data_ts, datetime):
                ts_dt = latest_data_ts
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=UTC)
            else:
                # date → end-of-day UTC for an upper-bound freshness read
                ts_dt = datetime.combine(
                    latest_data_ts, datetime.min.time(), tzinfo=UTC,
                )
            age = max(0.0, (now_utc - ts_dt).total_seconds())
            INGEST_DATA_FRESHNESS_SECONDS.labels(job_id=job_id).set(age)
    except Exception as exc:
        # Metrics are best-effort: if the prometheus_client registry
        # isn't importable in a test fixture, don't break the task.
        log.debug("ingest.health.metrics_failed",
                  extra={"job_id": job_id, "error": str(exc)})


async def get_health(job_id: str) -> IngestHealth | None:
    raw = await cache_get(_HEALTH_KEY_PREFIX + job_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return IngestHealth(
        job_id=data.get("job_id", job_id),
        last_run_at=data.get("last_run_at"),
        ok=bool(data.get("ok", False)),
        row_count=int(data.get("row_count", 0)),
        error=data.get("error"),
        silent_deny=data.get("silent_deny"),
        latest_data_ts=data.get("latest_data_ts"),
    )


# ── Failure backoff (per-job) ──────────────────────────────────────
#
# When an ingest task fails repeatedly (e.g. FinMind down, token
# revoked) we want to back off from the upstream instead of hammering
# it every cycle. Two Redis keys per job:
#
#   ingest:failures:{job_id}    integer counter (TTL 7d so a weekend
#                               failure cluster ages out cleanly)
#   ingest:backoff:{job_id}     marker key with TTL == backoff window;
#                               while present, the task should skip
#
# Backoff schedule (exponential, hour-based, capped at 6h):
#   1 fail  → 1 h
#   2 fail  → 2 h
#   3 fail  → 4 h
#   4 fail+ → 6 h
#
# A successful run clears both keys via `clear_failures(job_id)`.

_FAILURE_KEY_PREFIX = "ingest:failures:"
_BACKOFF_KEY_PREFIX = "ingest:backoff:"
_FAILURE_TTL = 7 * 24 * 3600
_BACKOFF_MAX_SECONDS = 6 * 3600


def _backoff_seconds_for(failures: int) -> int:
    """Exponential 2^(N-1) hours, capped at 6 h. N=1 → 1h, N=4+ → 6h."""
    if failures < 1:
        return 0
    return min(3600 * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)


async def record_failure(job_id: str) -> int:
    """Bump the failure counter and arm the backoff window. Returns the
    new failure count so callers can include it in their health string.
    Falls back to 1 (and skips backoff) if Redis is unreachable — in that
    case the next scheduled run will still try, mirroring the rest of the
    cache layer's "fall open on Redis outage" pattern."""
    try:
        new_count = await cache_incr(
            _FAILURE_KEY_PREFIX + job_id, ttl_seconds=_FAILURE_TTL,
        )
    except Exception as exc:
        log.warning("ingest.backoff.incr_failed",
                    extra={"job_id": job_id, "error": str(exc)})
        return 1
    backoff = _backoff_seconds_for(new_count)
    if backoff > 0:
        try:
            await cache_set(_BACKOFF_KEY_PREFIX + job_id, "1", backoff)
        except Exception as exc:
            log.warning("ingest.backoff.set_failed",
                        extra={"job_id": job_id, "error": str(exc)})
    return int(new_count)


async def clear_failures(job_id: str) -> None:
    """Reset failure counter + arm. Called on a successful run so a job
    that resumed working stops being throttled."""
    for key in (_FAILURE_KEY_PREFIX + job_id, _BACKOFF_KEY_PREFIX + job_id):
        try:
            await cache_delete(key)
        except Exception as exc:
            log.warning("ingest.backoff.clear_failed",
                        extra={"job_id": job_id, "key": key, "error": str(exc)})


async def backoff_remaining_seconds(job_id: str) -> int:
    """How many seconds until the backoff window expires. 0 means "go
    ahead and run". Uses Redis TTL on the marker key."""
    try:
        r = await get_redis()
        ttl = await r.ttl(_BACKOFF_KEY_PREFIX + job_id)
    except Exception as exc:
        log.warning("ingest.backoff.ttl_failed",
                    extra={"job_id": job_id, "error": str(exc)})
        return 0
    return max(0, int(ttl)) if ttl is not None else 0


async def get_failure_count(job_id: str) -> int:
    try:
        raw = await cache_get(_FAILURE_KEY_PREFIX + job_id)
    except Exception:
        return 0
    if not raw:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def list_health() -> list[IngestHealth]:
    """Scan Redis for every recorded job and return a stable-ordered list.

    Falls back to an empty list if Redis is unreachable so the admin
    endpoint stays available during cache outages.
    """
    try:
        r = await get_redis()
        keys = []
        async for key in r.scan_iter(match=_HEALTH_KEY_PREFIX + "*"):
            keys.append(key)
    except Exception as exc:
        log.warning("ingest.health.scan_failed", extra={"error": str(exc)})
        return []

    out: list[IngestHealth] = []
    for k in sorted(keys):
        job_id = k.removeprefix(_HEALTH_KEY_PREFIX) if isinstance(k, str) else k
        h = await get_health(job_id)
        if h is not None:
            out.append(h)
    return out


# ── Sparkline aggregation ─────────────────────────────────────────


@dataclass
class IngestHealthHistoryDay:
    """One day's outcome roll-up for a single job. Days with zero
    runs are omitted from the result list — the frontend treats a
    gap as "no data" automatically."""
    date: str          # ISO date in UTC
    ok: int = 0
    silent_deny: int = 0
    failed: int = 0
    skipped: int = 0


async def get_health_history(
    job_id: str, *, days: int = 7,
) -> list[IngestHealthHistoryDay]:
    """Daily outcome counts for `job_id` over the last `days` UTC
    calendar days, oldest-first.

    Days with zero runs are omitted — the UI's sparkline renders a
    gray "no data" cell for any missing date in the window. The
    aggregation runs entirely in SQL (`GROUP BY date(recorded_at)`)
    so a 7-day query stays a single index range scan + group-by.

    Returns an empty list (rather than raising) when the DB is
    unreachable, mirroring `list_health`'s fail-soft behaviour so
    the admin endpoint doesn't 500 on a transient blip.
    """
    if days <= 0:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    try:
        async with AsyncSessionLocal() as db:
            from models.ingest_health_history import IngestHealthHistory
            day_col = func.date(IngestHealthHistory.recorded_at)
            stmt = (
                select(
                    day_col.label("d"),
                    IngestHealthHistory.outcome,
                    func.count().label("n"),
                )
                .where(
                    IngestHealthHistory.job_id == job_id,
                    IngestHealthHistory.recorded_at >= cutoff,
                )
                .group_by(day_col, IngestHealthHistory.outcome)
                .order_by(day_col)
            )
            rows = (await db.execute(stmt)).all()
    except Exception as exc:
        log.warning(
            "ingest.health.history_query_failed",
            extra={"job_id": job_id, "error": str(exc)},
        )
        return []

    by_day: dict[str, IngestHealthHistoryDay] = {}
    for d, outcome, n in rows:
        # `func.date(...)` returns a `date` on Postgres and a string on
        # SQLite; normalize so the API always emits ISO-string dates.
        day_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        bucket = by_day.setdefault(day_str, IngestHealthHistoryDay(date=day_str))
        if outcome == "ok":
            bucket.ok = int(n)
        elif outcome == "silent_deny":
            bucket.silent_deny = int(n)
        elif outcome == "failed":
            bucket.failed = int(n)
        elif outcome == "skipped":
            bucket.skipped = int(n)
    return [by_day[k] for k in sorted(by_day.keys())]


# ── Convenience: open a new session for ad-hoc reads ───────────────

async def read_ohlcv_range_autosession(
    market: str, symbol: str, start: date, end: date,
) -> list[dict[str, Any]]:
    """Same as `read_ohlcv_range` but opens its own DB session.

    Used by `tw_market_service` whose existing read path doesn't already
    have a session in scope. DB errors are caught and logged so the
    service can fall through to the upstream waterfall — DB outages
    must never break the read path.
    """
    try:
        async with AsyncSessionLocal() as db:
            return await read_ohlcv_range(db, market, symbol, start, end)
    except Exception as exc:
        log.warning("ingest.read.db_error",
                    extra={"market": market, "symbol": symbol, "error": str(exc)})
        return []


async def upsert_ohlcv_bars_autosession(bars: Iterable[OhlcvBar]) -> int:
    """Same as `upsert_ohlcv_bars` but opens its own DB session.

    Used by `tw_market_service.get_history` to write back the upstream
    response without threading a session through the call chain. Errors
    are swallowed because the read path's primary obligation is to
    return data — DB writes are best-effort.
    """
    bars = list(bars)
    if not bars:
        return 0
    try:
        async with AsyncSessionLocal() as db:
            return await upsert_ohlcv_bars(db, bars)
    except Exception as exc:
        log.warning("ingest.write.db_error",
                    extra={"market": bars[0].market, "count": len(bars), "error": str(exc)})
        return 0


# ── PR #282: tw_stock_futures_oi ─────────────────────────────────


@dataclass(frozen=True)
class StockFuturesOiRow:
    """One per-stock-futures aggregated row (all 3 institutional
    types collapsed into columns, mirroring InstitutionalDailyRow).
    The cron builds these from FinMind's per-investor-type rows."""
    market: str
    symbol: str
    contract_id: str
    ts: date
    fini_long_oi: int | None
    fini_short_oi: int | None
    fini_net_oi: int | None
    sitc_long_oi: int | None
    sitc_short_oi: int | None
    sitc_net_oi: int | None
    dealer_long_oi: int | None
    dealer_short_oi: int | None
    dealer_net_oi: int | None
    source: str


_STOCK_FUTURES_OI_FIELDS = (
    "market", "symbol", "contract_id", "ts",
    "fini_long_oi", "fini_short_oi", "fini_net_oi",
    "sitc_long_oi", "sitc_short_oi", "sitc_net_oi",
    "dealer_long_oi", "dealer_short_oi", "dealer_net_oi",
    "source",
)


async def upsert_stock_futures_oi(
    db: AsyncSession, rows: Iterable[StockFuturesOiRow],
) -> int:
    """Bulk upsert. ON CONFLICT (market, symbol, ts) updates every
    metric column + contract_id + source so a re-ingest on the
    same date overwrites stale values (e.g. when FinMind corrects
    a prior session's OI numbers)."""
    payload = [
        _row_to_dict(r, fields=_STOCK_FUTURES_OI_FIELDS) for r in rows
    ]
    return await _chunked_upsert(
        db,
        model=TwStockFuturesOi,
        payload=payload,
        index_elements=["market", "symbol", "ts"],
        update_cols=(
            "contract_id",
            "fini_long_oi", "fini_short_oi", "fini_net_oi",
            "sitc_long_oi", "sitc_short_oi", "sitc_net_oi",
            "dealer_long_oi", "dealer_short_oi", "dealer_net_oi",
            "source",
        ),
    )


async def read_top_foreign_stock_futures_buyers(
    db: AsyncSession, *,
    market: str = "TW",
    days: int = 5,
    limit: int = 10,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Top N stocks by foreign-investor net-OI **change** over the
    last `days` trading days ending at `as_of` (default: today).

    Computes (latest_fini_net_oi - earliest_fini_net_oi) for each
    symbol that has both endpoints and ranks descending. Negative
    deltas (foreign net-OI shrinking, i.e. shorts building) are
    NOT inverted — the read tier returns the most-bullish only.
    Caller (context block) can call again with a different sort
    if it wants the bearish side too.

    Backtest look-ahead protection (PR #282 mirrors PR #243): when
    `as_of` is set, NOTHING is masked — futures OI rows are
    insert-only on FinMind's side (no retroactive corrections to
    long/short/net), and the cron ingests one-row-per-day with
    immutable historical numbers. Personas reading at as_of=D see
    exactly the OI snapshot that was published on D's close.
    """
    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 2)   # generous: covers weekends
    stmt = (
        select(
            TwStockFuturesOi.symbol,
            TwStockFuturesOi.ts,
            TwStockFuturesOi.fini_net_oi,
            TwStockFuturesOi.fini_long_oi,
            TwStockFuturesOi.fini_short_oi,
            TwStockFuturesOi.contract_id,
        )
        .where(
            TwStockFuturesOi.market == market,
            TwStockFuturesOi.ts >= cutoff,
            TwStockFuturesOi.ts <= end,
            TwStockFuturesOi.fini_net_oi.isnot(None),
        )
        .order_by(
            TwStockFuturesOi.symbol.asc(),
            TwStockFuturesOi.ts.asc(),
        )
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    # Group by symbol, then take (latest, earliest) within the
    # window for each.
    by_symbol: dict[str, list[tuple[date, int, int | None, int | None, str]]] = {}
    for sym, ts, net, long_oi, short_oi, contract in rows:
        by_symbol.setdefault(sym, []).append(
            (ts, int(net), long_oi, short_oi, contract),
        )

    results: list[dict[str, Any]] = []
    for sym, series in by_symbol.items():
        if len(series) < 2:
            # Need at least 2 points to compute a delta; one-row
            # symbols (just-listed contracts) get skipped silently.
            continue
        series.sort(key=lambda x: x[0])
        first_ts, first_net, *_ = series[0]
        last_ts, last_net, last_long, last_short, last_contract = series[-1]
        if last_ts == first_ts:
            continue
        delta = last_net - first_net
        results.append({
            "symbol":          sym,
            "contract_id":     last_contract,
            "fini_net_oi":     last_net,
            "fini_long_oi":    last_long,
            "fini_short_oi":   last_short,
            "fini_change":     delta,
            "as_of":           last_ts.isoformat(),
            "from_ts":         first_ts.isoformat(),
        })

    results.sort(key=lambda r: r["fini_change"], reverse=True)
    return results[:limit]


# ── PR #283: tw_vix_daily ────────────────────────────────────────


@dataclass(frozen=True)
class VixDailyRow:
    """One day's TAIWAN VIX close."""
    market: str
    ts: date
    vix_value: float
    source: str


_VIX_FIELDS = ("market", "ts", "vix_value", "source")


async def upsert_tw_vix_daily(
    db: AsyncSession, rows: Iterable[VixDailyRow],
) -> int:
    """Bulk upsert. ON CONFLICT (market, ts) updates `vix_value` +
    `source` so a re-pull of an already-ingested day with corrected
    TAIFEX numbers overwrites in place."""
    from models.tw_vix_daily import TwVixDaily
    payload = [_row_to_dict(r, fields=_VIX_FIELDS) for r in rows]
    return await _chunked_upsert(
        db,
        model=TwVixDaily,
        payload=payload,
        index_elements=["market", "ts"],
        update_cols=("vix_value", "source"),
    )


async def read_tw_vix_snapshot(
    db: AsyncSession, *,
    market: str = "TW",
    days: int = 5,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Latest VIX value + value `days` trading-days ago (best-effort
    via calendar-day lookback) + change %.

    Returns None when the archive has no rows in the window — caller
    (ctx block) drops the field entirely so personas don't reference
    a phantom value.

    Backtest semantics: when `as_of` is set, both endpoints are
    clamped to `<= as_of`. The TAIFEX archive is insert-only (VIX
    closes don't get retroactively corrected the way some FinMind
    datasets do), so no extra masking needed.
    """
    from models.tw_vix_daily import TwVixDaily

    end = as_of or date.today()
    cutoff = end - timedelta(days=days * 3 + 14)
    stmt = (
        select(TwVixDaily.ts, TwVixDaily.vix_value)
        .where(
            TwVixDaily.market == market,
            TwVixDaily.ts >= cutoff,
            TwVixDaily.ts <= end,
        )
        .order_by(TwVixDaily.ts.asc())
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None

    series = [(r[0], float(r[1])) for r in rows]
    last_ts, last_value = series[-1]

    # Pick the closest bar at-or-before `end - days` calendar days.
    # We over-approximate by iterating from oldest to newest because
    # the series is short (< 25 rows even at days=5 + 14 buffer).
    target = last_ts - timedelta(days=days)
    prev_pair = None
    for ts, val in series:
        if ts <= target:
            prev_pair = (ts, val)
        else:
            break

    change_pct: float | None = None
    if prev_pair is not None and prev_pair[1]:
        change_pct = round(
            (last_value / prev_pair[1] - 1) * 100, 4,
        )

    return {
        "as_of":      last_ts.isoformat(),
        "value":      round(last_value, 4),
        "from_ts":    prev_pair[0].isoformat() if prev_pair else None,
        "from_value": round(prev_pair[1], 4) if prev_pair else None,
        "change_pct": change_pct,
    }
