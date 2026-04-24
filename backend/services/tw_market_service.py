"""
Taiwan market service — owns all caching and waterfall fallback logic.
Data source priority:
  quote/OHLCV    : TWSE OpenAPI → FinMind
  institutional  : TWSE OpenAPI → FinMind
  margin         : TWSE OpenAPI → FinMind
  monthly revenue: FinMind → MOPS scraper
  financials     : FinMind only

Timezone: Taiwan is UTC+8, no DST. All API responses are tagged with
tz="Asia/Taipei" so the frontend can display correct local labels.
"""
import json
from datetime import datetime, date, timedelta, timezone
from typing import Any

import pytz

from cache.redis_cache import (
    cache_get, cache_set,
    key_quote, key_history,
    key_institutional, key_margin, key_revenue,
)
import data.tw.twse_connector as twse
import data.tw.finmind_connector as finmind
import data.tw.mops_connector as mops

_TW = pytz.timezone("Asia/Taipei")

TTL_QUOTE        = 60          # 1 min (TWSE is ~3-5 min delayed anyway)
TTL_HISTORY      = 4 * 3600
TTL_INSTITUTIONAL = 4 * 3600
TTL_MARGIN       = 4 * 3600
TTL_REVENUE      = 12 * 3600
TTL_FUNDAMENTALS = 24 * 3600
TTL_SCREENER     = 10 * 60

# In-process symbol→exchange map; refreshed by scheduler (Phase 5)
_exchange_map: dict[str, str] = {}   # symbol → "TWSE" | "TPEx"


def _is_tw_market_open() -> bool:
    now = datetime.now(_TW)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    close_ = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return open_ <= now < close_


def _today_str() -> str:
    return date.today().isoformat()


def _start_date(months: int = 6) -> str:
    return (date.today() - timedelta(days=months * 30)).isoformat()


# ── Symbol exchange lookup ────────────────────────────────────────

def get_exchange(symbol: str) -> str:
    return _exchange_map.get(symbol, "TWSE")


async def refresh_symbol_map() -> None:
    """Called by scheduler daily. Builds symbol→exchange map."""
    global _exchange_map
    new_map: dict[str, str] = {}
    try:
        for row in await twse.get_all_twse_symbols():
            code = row.get("Code") or row.get("證券代號", "")
            if code:
                new_map[code.strip()] = "TWSE"
    except Exception:
        pass
    try:
        for row in await twse.get_all_tpex_symbols():
            code = row.get("SecuritiesCompanyCode") or row.get("代號", "")
            if code:
                new_map[code.strip()] = "TPEx"
    except Exception:
        pass
    if new_map:
        _exchange_map = new_map


# ── Quote ─────────────────────────────────────────────────────────

async def get_quote(symbol: str) -> dict[str, Any]:
    key = key_quote("tw", symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    raw = None
    try:
        raw = await twse.get_realtime_quote(symbol)
    except Exception:
        pass

    if not raw:
        # FinMind fallback: latest close from last 5 days
        try:
            start = (date.today() - timedelta(days=7)).isoformat()
            bars = await finmind.get_daily_ohlcv(symbol, start)
            if bars:
                latest = bars[-1]
                raw = {
                    "symbol": symbol, "name_zh": "",
                    "close": latest["close"], "change": None,
                    "volume": latest["volume"],
                    "open": latest["open"], "high": latest["high"], "low": latest["low"],
                }
        except Exception:
            pass

    result = _normalize_quote(symbol, raw or {})
    await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


def _normalize_quote(symbol: str, raw: dict) -> dict[str, Any]:
    close = raw.get("close") or raw.get("price", 0) or 0
    prev  = raw.get("prev_close", 0) or 0
    chg   = raw.get("change") if raw.get("change") is not None else (close - prev if prev else None)
    chg_pct = round(chg / prev * 100, 4) if (chg is not None and prev) else None
    return {
        "symbol":        symbol,
        "market":        "TW",
        "exchange":      get_exchange(symbol),
        "name_zh":       raw.get("name_zh", ""),
        "price":         close,
        "change":        chg,
        "change_pct":    chg_pct,
        "volume":        raw.get("volume", 0),
        "open":          raw.get("open"),
        "high":          raw.get("high"),
        "low":           raw.get("low"),
        "currency":      "TWD",
        "ts":            int(datetime.now(timezone.utc).timestamp() * 1000),
        "tz":            "Asia/Taipei",
        "is_market_open": _is_tw_market_open(),
    }


# ── History ───────────────────────────────────────────────────────

async def get_history(symbol: str, months: int = 12) -> list[dict[str, Any]]:
    key = key_history("tw", symbol, "1d")
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    bars: list[dict] = []
    start = _start_date(months)

    # Try TWSE month-by-month (going back `months` months)
    try:
        for i in range(months):
            d = date.today().replace(day=1) - timedelta(days=i * 30)
            month_bars = await twse.get_daily_ohlcv(symbol, d)
            bars = month_bars + bars
    except Exception:
        bars = []

    if not bars:
        try:
            bars = await finmind.get_daily_ohlcv(symbol, start)
        except Exception:
            pass

    await cache_set(key, json.dumps(bars), TTL_HISTORY)
    return bars


# ── Institutional investors ───────────────────────────────────────

async def get_institutional(symbol: str, days: int = 30) -> list[dict[str, Any]]:
    key = key_institutional(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    result: list[dict] = []

    # TWSE returns all stocks for one day; we'd need to call per day
    # so default to FinMind which returns per-symbol range
    start = (date.today() - timedelta(days=days)).isoformat()
    try:
        result = await finmind.get_institutional(symbol, start)
    except Exception:
        pass

    if not result:
        # TWSE fallback: today only
        try:
            all_rows = await twse.get_institutional()
            result = [r for r in all_rows if r.get("symbol") == symbol]
        except Exception:
            pass

    await cache_set(key, json.dumps(result), TTL_INSTITUTIONAL)
    return result


# ── Margin balance ────────────────────────────────────────────────

async def get_margin(symbol: str, days: int = 30) -> list[dict[str, Any]]:
    key = key_margin(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    start = (date.today() - timedelta(days=days)).isoformat()
    result: list[dict] = []
    try:
        result = await finmind.get_margin(symbol, start)
    except Exception:
        pass

    if not result:
        try:
            all_rows = await twse.get_margin()
            result = [r for r in all_rows if r.get("symbol") == symbol]
        except Exception:
            pass

    await cache_set(key, json.dumps(result), TTL_MARGIN)
    return result


# ── Monthly revenue (月營收) ──────────────────────────────────────

async def get_revenue(symbol: str, months: int = 12) -> list[dict[str, Any]]:
    key = key_revenue(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    start = _start_date(months)
    result: list[dict] = []
    try:
        result = await finmind.get_monthly_revenue(symbol, start)
    except Exception:
        pass

    if not result:
        # MOPS fallback: current month only
        try:
            today = date.today()
            result = await mops.get_monthly_revenue(symbol, today.year, today.month)
        except Exception:
            pass

    await cache_set(key, json.dumps(result), TTL_REVENUE)
    return result


# ── Financials ────────────────────────────────────────────────────

async def get_fundamentals(symbol: str) -> dict[str, Any]:
    """PE, PB, dividend yield from TWSE BWIBBU_d + exchange info."""
    key = f"tw:fundamentals:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    result: dict[str, Any] = {
        "symbol": symbol,
        "market": "TW",
        "exchange": get_exchange(symbol),
    }
    try:
        ratios = await twse.get_valuation_ratios(symbol)
        result.update(ratios)
    except Exception:
        pass

    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


async def get_financials(symbol: str) -> list[dict[str, Any]]:
    key = f"tw:financials:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)
    result = await finmind.get_financials(symbol)
    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


# ── Screener ──────────────────────────────────────────────────────

async def get_screener(
    exchange: str | None = None,    # "TWSE" | "TPEx" | None (both)
    min_volume: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    key = f"tw:screener:{exchange}:{min_volume}:{limit}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        all_stocks = await twse.get_all_twse_symbols()
    except Exception:
        all_stocks = []

    result = []
    for s in all_stocks:
        code = s.get("Code") or s.get("證券代號", "")
        if not code:
            continue
        vol = twse._tw_int(s.get("成交股數") or s.get("TradeVolume", "0"))
        if min_volume and vol < min_volume:
            continue
        exch = _exchange_map.get(code.strip(), "TWSE")
        if exchange and exch != exchange:
            continue
        result.append({
            "symbol":   code.strip(),
            "market":   "TW",
            "exchange": exch,
            "name_zh":  s.get("Name") or s.get("證券名稱", ""),
            "price":    twse._tw_num(s.get("收盤價") or s.get("ClosingPrice")),
            "volume":   vol,
        })
        if len(result) >= limit:
            break

    await cache_set(key, json.dumps(result), TTL_SCREENER)
    return result


# ── TAIEX index ───────────────────────────────────────────────────

async def get_index() -> dict[str, Any]:
    key = "tw:index:taiex"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)
    result = await twse.get_taiex()
    await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


# ── News ──────────────────────────────────────────────────────────

TTL_NEWS = 5 * 60


async def get_news(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch news for a TW stock via yfinance ({symbol}.TW format)."""
    key = f"tw:news:{symbol.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    import asyncio
    from datetime import datetime, timezone
    loop = asyncio.get_running_loop()

    def _fetch():
        import yfinance as yf
        ticker_str = f"{symbol}.TW"
        t = yf.Ticker(ticker_str)
        raw = t.news or []
        items = []
        for n in raw[:limit]:
            thumbnail = None
            if t_data := n.get("thumbnail"):
                resolutions = t_data.get("resolutions", [])
                thumbnail = resolutions[0].get("url") if resolutions else None
            items.append({
                "title":     n.get("title", ""),
                "publisher": n.get("publisher", ""),
                "link":      n.get("link", ""),
                "published_at": datetime.fromtimestamp(
                    n.get("providerPublishTime", 0), tz=timezone.utc
                ).isoformat(),
                "thumbnail": thumbnail,
            })
        return items

    result = await loop.run_in_executor(None, _fetch)
    if result:
        await cache_set(key, json.dumps(result), TTL_NEWS)
    return result
