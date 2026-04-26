"""
FinMind connector — free tier: 600 req/day without token.
Tracks usage in Redis; falls silent (returns []) when near the limit.
"""
import httpx
from typing import Any

from cache.redis_cache import cache_incr, key_finmind_counter
from config import settings

_BASE = "https://api.finmindtrade.com/api/v4/data"


async def _query(dataset: str, data_id: str, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    """
    Check daily quota before hitting the API.
    Returns [] when quota is exhausted so callers fall back to TWSE.
    """
    # 86400s = 24h; counter auto-expires so no explicit reset needed
    count = await cache_incr(key_finmind_counter(), ttl_seconds=86400)
    if count > settings.FINMIND_DAILY_REQUEST_LIMIT:
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
