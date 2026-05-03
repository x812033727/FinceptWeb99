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

from sqlalchemy import delete, or_ as sa_or, select
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
        can `filter(None, ...)` instead of try/except per row."""
        ts_raw = row.get("time") or row.get("date")
        if not ts_raw:
            return None
        try:
            ts = date.fromisoformat(str(ts_raw)[:10])
        except ValueError:
            return None
        return cls(
            market=market,
            symbol=symbol,
            ts=ts,
            open=_to_float(row.get("open")),
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=_to_float(row.get("close")),
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
    `tw_market_service.get_revenue` callers."""
    return {
        "date":        r.ts.isoformat(),
        "symbol":      r.symbol,
        "revenue":     int(r.revenue) if r.revenue is not None else 0,
        "revenue_yoy": float(r.revenue_yoy) if r.revenue_yoy is not None else 0.0,
        "revenue_mom": float(r.revenue_mom) if r.revenue_mom is not None else 0.0,
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
    rows_sorted = sorted(
        rows,
        key=lambda r: float(r.revenue_yoy) if r.revenue_yoy is not None else -1e9,
        reverse=True,
    )
    return [
        {
            "symbol":      r.symbol,
            "ts":          r.ts.isoformat(),
            "revenue":     int(r.revenue) if r.revenue is not None else 0,
            "revenue_yoy": float(r.revenue_yoy) if r.revenue_yoy is not None else None,
            "revenue_mom": float(r.revenue_mom) if r.revenue_mom is not None else None,
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
    the upstream field blank for fresh announcements)."""
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
    return [
        {
            "symbol":         r.symbol,
            "period_start":   r.period_start.isoformat(),
            "period_end":     r.period_end.isoformat() if r.period_end else None,
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
    return [
        {
            "symbol": r.symbol,
            "date":   r.ts.isoformat(),
            "status": r.status,
            "reason": r.reason,
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
    duplicates are silently dropped at the DB layer)."""
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
    if dialect == "sqlite":
        stmt = sqlite_insert(NewsArticle).values(payload)
    else:
        stmt = pg_insert(NewsArticle).values(payload)
    stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_hash"])
    await db.execute(stmt)
    await db.commit()
    return len(payload)


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


# ── Health snapshot ────────────────────────────────────────────────

@dataclass
class IngestHealth:
    job_id: str
    last_run_at: str | None
    ok: bool
    row_count: int
    error: str | None


async def record_health(
    job_id: str, *, ok: bool, row_count: int = 0, error: str | None = None,
) -> None:
    """Persist a per-job health snapshot in Redis.

    Stored as a single JSON blob keyed by job_id with a long TTL so the
    admin dashboard can reflect "last successful run" even after a quiet
    weekend. A separate Postgres table would be more durable but isn't
    needed yet — Redis state is regenerated on the next scheduled run.
    """
    payload = json.dumps({
        "job_id": job_id,
        "last_run_at": datetime.now(UTC).isoformat(),
        "ok": ok,
        "row_count": int(row_count),
        "error": error,
    })
    try:
        await cache_set(_HEALTH_KEY_PREFIX + job_id, payload, _HEALTH_TTL)
    except Exception as exc:
        log.warning("ingest.health.record_failed",
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
