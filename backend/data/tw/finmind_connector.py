"""
FinMind connector — registered free tier: 600 req/HOUR (300/hr anonymous).
Tracks usage in Redis with a 1-hour rolling window; falls silent (returns
[]) when near the limit.
"""
import logging
from typing import Any

import httpx

from cache.redis_cache import cache_incr, key_finmind_counter
from config import settings

log = logging.getLogger(__name__)

_BASE = "https://api.finmindtrade.com/api/v4/data"


async def _resolve_hourly_limit() -> int:
    """Look up the runtime-tunable FinMind per-hour cap. The resolver
    caches in Redis for 60 s so a hot path doesn't pay a DB hit each
    call. Falls back to the compiled-in setting on any error so a
    transient DB outage doesn't break the connector."""
    try:
        from db.session import AsyncSessionLocal
        from services.runtime_config_service import get_int as _get_int
        async with AsyncSessionLocal() as db:
            return await _get_int(db, "FINMIND_HOURLY_REQUEST_LIMIT")
    except Exception:
        return settings.FINMIND_HOURLY_REQUEST_LIMIT


async def _get_token() -> str:
    """Resolve the active FinMind token.

    DB-managed value (AdminPage MarketKeysCard) wins; falls back to the
    .env-supplied `settings.FINMIND_TOKEN` when no DB row exists, and to
    `""` if even that's empty. Lifted to a module-level helper so unit
    tests can patch it without triggering the heavy `services.market_key_
    service` → `auth.llm_key_crypto` import chain.
    """
    try:
        from services.market_key_service import resolve_key
        return await resolve_key("finmind")
    except Exception:
        return settings.FINMIND_TOKEN or ""


async def _query(dataset: str, data_id: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    """
    Check hourly quota before hitting the API.
    Returns [] when quota is exhausted so callers fall back to TWSE.
    """
    token = await _get_token()

    # Cheap log when token is empty so operators see the correlation
    # in logs alongside any subsequent 400 / 401 from FinMind. Doesn't
    # short-circuit because some datasets (free-tier OHLCV) genuinely
    # work without a token; only paid ones (TaiwanStockNews etc.)
    # reject.
    if not token:
        log.debug("finmind.empty_token", extra={"dataset": dataset})

    # 3600s = 1h. FinMind's actual rate limit is per-hour (600/hr for
    # registered free tier, 300/hr anonymous), so the counter window
    # has to match — a 24h TTL was wasting 24× the available budget.
    count = await cache_incr(key_finmind_counter(), ttl_seconds=3600)
    if count > await _resolve_hourly_limit():
        return []

    params: dict = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "token": token,
    }
    if end_date:
        params["end_date"] = end_date

    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(_BASE, params=params)
        r.raise_for_status()
        body = r.json()

    if body.get("status") != 200:
        return []
    return body.get("data", [])


# ── Daily OHLCV ───────────────────────────────────────────────────

async def get_daily_ohlcv(symbol: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = await _query("TaiwanStockPrice", symbol, start_date, end_date)
    return [
        {
            "time":   r["date"],
            "open":   r.get("open"),
            "high":   r.get("max"),
            "low":    r.get("min"),
            "close":  r.get("close"),
            "volume": r.get("Trading_Volume", 0),
        }
        for r in rows
    ]


# ── Institutional investors ───────────────────────────────────────

async def get_institutional(symbol: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = await _query("TaiwanStockInstitutionalInvestors", symbol, start_date, end_date)
    # FinMind returns one row per investor type per date; pivot to one row per date
    from collections import defaultdict
    by_date: dict[str, dict] = defaultdict(dict)
    for r in rows:
        d = r["date"]
        name = r.get("name", "")
        buy  = r.get("buy", 0)
        sell = r.get("sell", 0)
        if "外資" in name:
            by_date[d]["fini_buy"]    = buy
            by_date[d]["fini_sell"]   = sell
        elif "投信" in name:
            by_date[d]["sitc_buy"]    = buy
            by_date[d]["sitc_sell"]   = sell
        elif "自營商" in name:
            by_date[d]["dealer_buy"]  = buy
            by_date[d]["dealer_sell"] = sell
    return [{"date": d, "symbol": symbol, **v} for d, v in sorted(by_date.items())]


# ── Margin balance ────────────────────────────────────────────────

async def get_margin(symbol: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = await _query("TaiwanStockMarginPurchaseShortSale", symbol, start_date, end_date)
    return [
        {
            "date":            r["date"],
            "symbol":          symbol,
            "margin_purchase": r.get("MarginPurchaseBuy", 0),
            "margin_balance":  r.get("MarginPurchaseBalance", 0),
            "short_sale":      r.get("ShortSaleSell", 0),
            "short_balance":   r.get("ShortSaleBalance", 0),
        }
        for r in rows
    ]


# ── Monthly revenue (月營收) ──────────────────────────────────────
#
# FinMind's `TaiwanStockMonthRevenue` row shape:
#   date          — report-publish date (YYYY-MM-DD), e.g. "2026-02-10"
#   stock_id      — symbol
#   revenue       — current month revenue (千元 NTD)
#   revenue_year  — REVENUE PERIOD YEAR (integer), e.g. 2026   (NOT YoY %!)
#   revenue_month — REVENUE PERIOD MONTH (integer 1-12)         (NOT MoM %!)
#
# The connector deliberately does NOT expose `revenue_yoy` / `revenue_mom`
# from the upstream row because FinMind's `TaiwanStockMonthRevenue` basic
# dataset doesn't carry growth percentages — they're a derived metric.
# Earlier code mapped `revenue_year` → `revenue_yoy` thinking it was the
# growth rate, which produced absurd "+2026% YoY" values in production
# (PR #211). The ingest task computes YoY/MoM by joining historical rows
# in `tw_revenue_monthly`.


async def get_monthly_revenue(symbol: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = await _query("TaiwanStockMonthRevenue", symbol, start_date, end_date)
    return [
        {
            "date":         r["date"],
            "symbol":       symbol,
            "revenue":      r.get("revenue", 0),           # 千元 NTD
            "period_year":  r.get("revenue_year"),         # int, not %
            "period_month": r.get("revenue_month"),        # int 1-12, not %
        }
        for r in rows
    ]


async def get_monthly_revenue_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Pull TaiwanStockMonthRevenue with `data_id=""` to get every
    company's monthly revenue in a single FinMind call. Saves ~2000×
    quota vs per-symbol fan-out — used by the daily ingest task.

    Each FinMind row carries its own `stock_id`, so we don't have to
    thread a symbol through. Rows with missing stock_id are dropped
    rather than poisoning the upsert with empty PKs.
    """
    rows = await _query("TaiwanStockMonthRevenue", "", start_date, end_date)
    return [
        {
            "date":         r["date"],
            "symbol":       r.get("stock_id", ""),
            "revenue":      r.get("revenue", 0),           # 千元 NTD
            "period_year":  r.get("revenue_year"),         # int, not %
            "period_month": r.get("revenue_month"),        # int 1-12, not %
        }
        for r in rows
        if r.get("stock_id")
    ]


# ── Financials ────────────────────────────────────────────────────

async def get_financials(symbol: str, start_date: str = "2020-01-01") -> list[dict[str, Any]]:
    return await _query("TaiwanStockFinancialStatements", symbol, start_date)


async def get_balance_sheet(symbol: str, start_date: str = "2020-01-01") -> list[dict[str, Any]]:
    return await _query("TaiwanStockBalanceSheet", symbol, start_date)


async def get_cash_flow(symbol: str, start_date: str = "2020-01-01") -> list[dict[str, Any]]:
    return await _query("TaiwanStockCashFlowsStatement", symbol, start_date)


# ── Dividends ─────────────────────────────────────────────────────

async def get_dividends(symbol: str, start_date: str = "2018-01-01") -> list[dict[str, Any]]:
    """
    Cash + stock dividend history. Works for both ordinary stocks and ETFs
    (ETFs use the same TaiwanStockDividend dataset). Field names FinMind
    returns can vary by report year — we keep the raw rows and let the
    service normalize.
    """
    return await _query("TaiwanStockDividend", symbol, start_date)


# ── ETF holdings ──────────────────────────────────────────────────

async def get_etf_holdings(symbol: str, start_date: str = "2024-01-01") -> list[dict[str, Any]]:
    """
    ETF underlying-stock weights (持股明細). FinMind exposes this under
    the 付費專案 datasets, but the free tier returns the latest month
    for most popular ETFs. Returns [] silently if the dataset is not
    accessible — caller renders an empty state.
    """
    return await _query("TaiwanStockHoldingSharesPer", symbol, start_date)


# ── Holdings + market institutional aggregates (PR #193) ─────────

async def get_shareholding_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockShareholding` — share-distribution buckets per
    (symbol, date). FinMind's response shape varies; this returns
    the rows largely as-is and lets the cron's `_normalize_*`
    helpers fan them out into the long-form table.

    Sponsor-tier. Published weekly by TWSE/TPEx so a daily cron is
    fine; UPSERT is idempotent on the (symbol, ts, bucket_id) PK.
    """
    return await _query("TaiwanStockShareholding", "", start_date, end_date)


async def get_total_institutional_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockTotalInstitutionalInvestors` — full-market three-
    major-investor (foreign / SITC / dealer) daily aggregate. One
    row per trading day. Sponsor-tier.

    FinMind's row shape uses Chinese-keyed columns; we normalise
    to canonical English names here so the cron has stable input.
    """
    rows = await _query("TaiwanStockTotalInstitutionalInvestors", "", start_date, end_date)
    return [
        {
            "date":         r.get("date"),
            "name":         r.get("name") or "",
            # FinMind sometimes returns one row per investor type
            # ({date, name="Foreign_Investor", buy, sell}), other
            # times one row per date with all fields. Pass through;
            # the cron's `_aggregate_total_institutional` builds a
            # single row per date from whichever shape arrives.
            "buy":          r.get("buy") or r.get("buy_amount"),
            "sell":         r.get("sell") or r.get("sell_amount"),
        }
        for r in rows
    ]


# ── Risk-warning datasets (處置 / 暫停 / 當沖) ────────────────────

async def get_disposition_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockDispositionSecuritiesPeriod` — list of stocks
    placed under disposition (處置股) with their level + period
    window. Sponsor-tier. One market-wide call covers all active +
    recently-expired entries.
    """
    rows = await _query("TaiwanStockDispositionSecuritiesPeriod", "", start_date, end_date)
    return [
        {
            "symbol":         r.get("stock_id", ""),
            "period_start":   r.get("announcement_date") or r.get("period_start") or r.get("date"),
            "period_end":     r.get("end_date") or r.get("period_end"),
            "classification": r.get("classification") or r.get("category"),
            "level":          r.get("level") or r.get("disposition_level"),
            "reason":         r.get("reason") or r.get("disposition_reason"),
        }
        for r in rows
        if r.get("stock_id")
    ]


async def get_suspended_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockSuspended` — full-trading-halt event log. Rare;
    typical month has 0-3 entries. Sponsor-tier.
    """
    rows = await _query("TaiwanStockSuspended", "", start_date, end_date)
    return [
        {
            "symbol":  r.get("stock_id", ""),
            "date":    r.get("date"),
            "status":  r.get("status") or r.get("suspended_status"),
            "reason":  r.get("reason") or r.get("suspended_reason"),
        }
        for r in rows
        if r.get("stock_id")
    ]


async def get_day_trading_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockDayTrading` — per-(symbol, date) day-trading
    volume aggregates. Used to spot speculative-favorite stocks
    where intraday round-trips dominate price action. Sponsor-tier.
    Heavier than the other two — ~1700 rows × N days per call.
    """
    rows = await _query("TaiwanStockDayTrading", "", start_date, end_date)
    return [
        {
            "symbol":      r.get("stock_id", ""),
            "date":        r.get("date"),
            # FinMind keys vary across versions; try common variants.
            "volume":      r.get("Volume") or r.get("TradeVolume") or r.get("volume"),
            "buy_amount":  r.get("BuyAfterSale") or r.get("buy_amount") or r.get("BuyAmount"),
            "sell_amount": r.get("SellAfterPurchase") or r.get("sell_amount") or r.get("SellAmount"),
        }
        for r in rows
        if r.get("stock_id") and r.get("date")
    ]


# ── Total return index (含息報酬指數) ────────────────────────────

async def get_taiex_total_return_index(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockTotalReturnIndex` (TAIEX TR variant `IR0001` —
    台灣加權股價報酬指數). Unlike the price-only TAIEX archived under
    `_TAIEX` in `ohlcv_daily`, this version reinvests dividends, so
    long-window comparisons against a TW portfolio are meaningfully
    closer to apples-to-apples.

    Sponsor-tier dataset. One call per ingest tick covers the whole
    archive; we run it daily.

    Output is shaped to match `OhlcvBar.from_connector_row` so the
    cron can upsert into `ohlcv_daily` under the synthetic symbol
    `_TAIEX_TR` (mirrors how PR #132 stores `_TAIEX`).
    """
    rows = await _query("TaiwanStockTotalReturnIndex", "IR0001", start_date, end_date)
    return [
        {
            "time":   r.get("date"),
            # Index has no real OHL — flatten everything to close.
            # Same shape choice as `_TAIEX` price index.
            "open":   r.get("price") or r.get("close"),
            "high":   r.get("price") or r.get("close"),
            "low":    r.get("price") or r.get("close"),
            "close":  r.get("price") or r.get("close"),
            # Volume is not meaningful for an index; reuse the slot
            # for the day's traded amount if FinMind exposes it.
            "volume": r.get("trading_volume") or 0,
        }
        for r in rows
        if r.get("date") and (r.get("price") or r.get("close"))
    ]


# ── Government bank daily flow (八大行庫) ─────────────────────────

async def get_government_bank_flow_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """`TaiwanStockGovernmentBankBuySell` — eight government banks'
    daily buy/sell aggregates in TW listed equities. One row per
    `(date, bank_name)` upstream; we pass through that shape and
    let `tasks.ingest_govt_bank_flow_tw` upsert into
    `tw_govt_bank_flow_daily`.

    Sponsor-tier dataset. Empty `data_id` returns the full eight-bank
    set; one call per day suffices.
    """
    rows = await _query("TaiwanStockGovernmentBankBuySell", "", start_date, end_date)
    return [
        {
            "date":        r.get("date"),
            "bank_name":   r.get("name") or r.get("bank_name"),
            "buy_amount":  r.get("buy_amount") or r.get("buy"),
            "sell_amount": r.get("sell_amount") or r.get("sell"),
        }
        for r in rows
        if (r.get("name") or r.get("bank_name"))
    ]


# ── Buyback announcements (庫藏股) ───────────────────────────────

async def get_buyback_market_wide(
    start_date: str, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Pull TaiwanStockBuyBack with `data_id=""` to get every
    listed company's buyback announcements for the date range. One
    row per (symbol, announce_date); FinMind updates the
    `current_buy_back_shares` field day-by-day as execution
    progresses, so re-pulling overwrites stale execution counts.

    Sponsor-tier dataset as of 2026-04. The shared FinMind paywall
    detector in `data.tw.finmind_paywall` covers the failure mode
    when called by an unsponsored token.

    Returns a list of canonical-shape dicts. Field naming follows
    FinMind's response keys; `_normalize_buyback_row` in
    `tasks.ingest_buyback_tw` does the conversion to the DB row
    shape (with date parsing + null tolerance).
    """
    rows = await _query("TaiwanStockBuyBack", "", start_date, end_date)
    return [
        {
            "date":          r.get("date"),
            "symbol":        r.get("stock_id", ""),
            "method":        r.get("buy_back_method"),
            "period_start":  r.get("buy_back_period_start"),
            "period_end":    r.get("buy_back_period_end"),
            "purpose":       r.get("buy_back_purpose"),
            "max_shares":    r.get("buy_back_max_shares") or r.get("max_buy_back_shares"),
            "current_shares": r.get("current_buy_back_shares"),
            "price_lower":   r.get("buy_back_price_lower"),
            "price_upper":   r.get("buy_back_price_upper"),
        }
        for r in rows
        if r.get("stock_id")
    ]


# ── News ──────────────────────────────────────────────────────────

async def get_news(
    start_date: str,
    symbol: str = "",
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """
    Taiwan stock news. `symbol=""` returns market-wide articles
    (FinMind's data_id="" returns everyone's news in one call — a
    huge quota saving versus per-symbol fan-out). Pass a specific
    symbol when only that issuer's headlines are needed.

    `end_date` (PR #212) bounds the response on the server side, so
    historical backfills can chunk by month without pulling everything
    after `start_date` and post-filtering. Required by the paid-tier
    backfill script in `scripts/backfill_news_finmind.py`.

    Returns [] silently when FinMind quota is exhausted; the daily
    ingest task records this via `ingest_health` so operators can
    see degraded mode in the admin dashboard.
    """
    rows = await _query("TaiwanStockNews", symbol, start_date, end_date)
    return [
        {
            "title":        r.get("title", ""),
            "link":         r.get("link", ""),
            "source_name":  r.get("source", ""),
            "description":  r.get("description", ""),
            "published_at": r.get("date", ""),
            "symbol":       r.get("stock_id", "") or None,
        }
        for r in rows
    ]
