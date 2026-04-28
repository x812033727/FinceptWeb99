"""
FinMind connector — free tier: 600 req/day without token.
Tracks usage in Redis; falls silent (returns []) when near the limit.
"""
from typing import Any

import httpx

from cache.redis_cache import cache_incr, key_finmind_counter
from config import settings

_BASE = "https://api.finmindtrade.com/api/v4/data"


async def _resolve_daily_limit() -> int:
    """Look up the runtime-tunable FinMind daily cap. The resolver caches
    in Redis for 60 s so a hot path doesn't pay a DB hit each call. Falls
    back to the compiled-in setting on any error so a transient DB
    outage doesn't break the connector."""
    try:
        from db.session import AsyncSessionLocal
        from services.runtime_config_service import get_int as _get_int
        async with AsyncSessionLocal() as db:
            return await _get_int(db, "FINMIND_DAILY_REQUEST_LIMIT")
    except Exception:
        return settings.FINMIND_DAILY_REQUEST_LIMIT


async def _query(dataset: str, data_id: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    """
    Check daily quota before hitting the API.
    Returns [] when quota is exhausted so callers fall back to TWSE.
    """
    # 86400s = 24h; counter auto-expires so no explicit reset needed
    count = await cache_incr(key_finmind_counter(), ttl_seconds=86400)
    if count > await _resolve_daily_limit():
        return []

    params: dict = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
        "token": settings.FINMIND_TOKEN,
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

async def get_monthly_revenue(symbol: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = await _query("TaiwanStockMonthRevenue", symbol, start_date, end_date)
    return [
        {
            "date":          r["date"],
            "symbol":        symbol,
            "revenue":       r.get("revenue", 0),           # 千元 NTD
            "revenue_mom":   r.get("revenue_month", 0),     # 月增率 %
            "revenue_yoy":   r.get("revenue_year", 0),      # 年增率 %
        }
        for r in rows
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


# ── News ──────────────────────────────────────────────────────────

async def get_news(start_date: str, symbol: str = "") -> list[dict[str, Any]]:
    """
    Taiwan stock news. `symbol=""` returns market-wide articles
    (FinMind's data_id="" returns everyone's news in one call — a
    huge quota saving versus per-symbol fan-out). Pass a specific
    symbol when only that issuer's headlines are needed.

    Returns [] silently when FinMind quota is exhausted; the daily
    ingest task records this via `ingest_health` so operators can
    see degraded mode in the admin dashboard.
    """
    rows = await _query("TaiwanStockNews", symbol, start_date)
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
