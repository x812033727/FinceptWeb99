"""TW chip metrics: 法人/融資融券, 八大行庫, 股權分散, 全市場三大法人."""
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_chip_metrics import TwInstitutionalDaily, TwMarginDaily
from models.tw_govt_bank_flow import TwGovtBankFlowDaily
from models.tw_holdings_aggregates import (
    TwMarketInstitutionalDaily,
    TwStockShareholding,
)
from services.ingest.repo._common import _chunked_upsert, _row_to_dict

log = logging.getLogger(__name__)


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
