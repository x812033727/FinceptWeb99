"""
US market service — owns all caching and waterfall fallback logic.
Rules:
  - Always check Redis first
  - Polygon.io if API key is set, else yfinance
  - All timestamps returned as Unix ms UTC
  - Market-hours helper uses America/New_York tz
"""
import json
from datetime import datetime, timezone, date, timedelta
from typing import Any
import pytz

from cache.redis_cache import cache_get, cache_set, key_quote, key_history, key_fundamentals
from config import settings
import data.us.polygon_connector as polygon
import data.us.yfinance_connector as yfinance
from data.us.fred_connector import get_series, SERIES

_ET = pytz.timezone("America/New_York")

# ── TTLs (seconds) ────────────────────────────────────────────────
TTL_QUOTE       = 15
TTL_HISTORY     = 4 * 3600
TTL_FUNDAMENTALS = 24 * 3600
TTL_OPTIONS     = 5 * 60
TTL_SCREENER    = 10 * 60


def _is_market_open() -> bool:
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close


def _use_polygon() -> bool:
    return bool(settings.POLYGON_API_KEY)


# ── Quote ─────────────────────────────────────────────────────────

async def get_quote(ticker: str) -> dict[str, Any]:
    key = key_quote("us", ticker)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        raw = await polygon.get_quote(ticker) if _use_polygon() else await yfinance.get_quote(ticker)
    except Exception:
        raw = await yfinance.get_quote(ticker)

    result = _normalize_quote(ticker, raw)
    await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


def _normalize_quote(ticker: str, raw: dict) -> dict[str, Any]:
    return {
        "symbol": ticker.upper(),
        "market": "US",
        "name": raw.get("name", ticker.upper()),
        "price": raw.get("price", 0),
        "change": raw.get("change", 0),
        "change_pct": raw.get("change_pct", 0),
        "volume": raw.get("volume", 0),
        "open": raw.get("open"),
        "high": raw.get("high"),
        "low": raw.get("low"),
        "prev_close": raw.get("prev_close"),
        "market_cap": raw.get("market_cap"),
        "currency": "USD",
        "ts": raw.get("ts") or int(datetime.now(timezone.utc).timestamp() * 1000),
        "is_market_open": _is_market_open(),
    }


# ── History ───────────────────────────────────────────────────────

async def get_history(ticker: str, period: str = "1y", interval: str = "1d") -> list[dict[str, Any]]:
    key = key_history("us", ticker, interval)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        if _use_polygon():
            from_date, to_date = _period_to_dates(period)
            timespan = _interval_to_timespan(interval)
            bars = await polygon.get_aggs(ticker, from_date, to_date, timespan)
        else:
            bars = await yfinance.get_history(ticker, period=period, interval=interval)
    except Exception:
        bars = await yfinance.get_history(ticker, period=period, interval=interval)

    # Normalize time to "YYYY-MM-DD" string for daily, Unix ms for intraday
    result = _normalize_bars(bars, interval)
    await cache_set(key, json.dumps(result), TTL_HISTORY)
    return result


def _normalize_bars(bars: list[dict], interval: str) -> list[dict]:
    out = []
    for b in bars:
        t = b.get("time")
        if isinstance(t, int) and interval in ("1d", "1wk", "1mo"):
            t = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append({"time": t, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": b["volume"]})
    return out


def _period_to_dates(period: str) -> tuple[str, str]:
    today = date.today()
    delta_map = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825, "10y": 3650}
    days = delta_map.get(period, 365)
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _interval_to_timespan(interval: str) -> str:
    return {"1m": "minute", "5m": "minute", "15m": "minute", "1h": "hour", "1d": "day", "1wk": "week", "1mo": "month"}.get(interval, "day")


# ── Fundamentals ──────────────────────────────────────────────────

async def get_fundamentals(ticker: str) -> dict[str, Any]:
    key = key_fundamentals("us", ticker)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        if _use_polygon():
            details = await polygon.get_ticker_details(ticker)
            result = _normalize_fundamentals_polygon(ticker, details)
        else:
            raise Exception("no polygon key")
    except Exception:
        info = await yfinance.get_info(ticker)
        result = _normalize_fundamentals_yf(ticker, info)

    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


def _normalize_fundamentals_polygon(ticker: str, d: dict) -> dict[str, Any]:
    return {
        "symbol": ticker.upper(), "market": "US",
        "name": d.get("name"), "sector": d.get("sic_description"),
        "market_cap": d.get("market_cap"), "share_class_shares_outstanding": d.get("share_class_shares_outstanding"),
        "pe_ratio": None, "pb_ratio": None, "eps": None,
        "dividend_yield": None, "beta": None,
        "description": d.get("description"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalize_fundamentals_yf(ticker: str, info: dict) -> dict[str, Any]:
    return {
        "symbol": ticker.upper(), "market": "US",
        "name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"), "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE") or info.get("forwardPE"),
        "pb_ratio": info.get("priceToBook"),
        "eps": info.get("trailingEps"),
        "dividend_yield": info.get("dividendYield"),
        "beta": info.get("beta"),
        "52w_high": info.get("fiftyTwoWeekHigh"),
        "52w_low": info.get("fiftyTwoWeekLow"),
        "description": info.get("longBusinessSummary"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Financials ────────────────────────────────────────────────────

async def get_financials(ticker: str) -> dict[str, Any]:
    key = f"us:financials:{ticker}:annual"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        if _use_polygon():
            data = await polygon.get_financials(ticker)
            result = {"source": "polygon", "data": data}
        else:
            raise Exception("no polygon key")
    except Exception:
        data = await yfinance.get_financials(ticker)
        result = {"source": "yfinance", **data}

    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


# ── Options chain ─────────────────────────────────────────────────

async def get_options(ticker: str, expiration_date: str | None = None) -> list[dict[str, Any]]:
    key = f"us:options:{ticker}:{expiration_date or 'all'}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    if not _use_polygon():
        return []  # options require Polygon

    data = await polygon.get_options_chain(ticker, expiration_date)
    await cache_set(key, json.dumps(data), TTL_OPTIONS)
    return data


# ── Screener ──────────────────────────────────────────────────────

# S&P 500 tickers cached in-process (updated daily by scheduler in Phase 5)
_sp500_cache: list[str] = []

async def _get_sp500_tickers() -> list[str]:
    global _sp500_cache
    if _sp500_cache:
        return _sp500_cache
    # Fetch from Wikipedia (simple, no API key required)
    import httpx
    import re
    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    tickers = re.findall(r'<td><a[^>]+>([A-Z]{1,5})</a></td>', r.text)
    _sp500_cache = list(dict.fromkeys(tickers))[:505]
    return _sp500_cache


async def get_screener(
    min_market_cap: float | None = None,
    max_pe: float | None = None,
    min_volume: int | None = None,
    sector: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    key = f"us:screener:{min_market_cap}:{max_pe}:{min_volume}:{sector}:{limit}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    if _use_polygon():
        snapshots = await polygon.get_snapshot_all()
        results = _filter_polygon_snapshots(snapshots, min_market_cap, min_volume, limit)
    else:
        tickers = await _get_sp500_tickers()
        results = await _screener_yfinance(tickers, min_market_cap, max_pe, min_volume, sector, limit)

    await cache_set(key, json.dumps(results), TTL_SCREENER)
    return results


def _filter_polygon_snapshots(snaps: list[dict], min_cap, min_vol, limit) -> list[dict]:
    out = []
    for s in snaps:
        day = s.get("day", {})
        vol = day.get("v", 0)
        if min_vol and vol < min_vol:
            continue
        out.append({
            "symbol": s.get("ticker"),
            "market": "US",
            "name": s.get("details", {}).get("name", ""),
            "price": day.get("c", 0),
            "change_pct": s.get("todaysChangePerc", 0),
            "volume": vol,
        })
    return out[:limit]


async def _screener_yfinance(tickers, min_cap, max_pe, min_vol, sector, limit) -> list[dict]:
    import asyncio
    results = []

    async def _fetch_one(t: str):
        try:
            info = await yfinance.get_info(t)
            cap = info.get("marketCap", 0) or 0
            pe = info.get("trailingPE")
            vol = info.get("volume", 0) or 0
            sec = info.get("sector", "")
            if min_cap and cap < min_cap:
                return
            if max_pe and pe and pe > max_pe:
                return
            if min_vol and vol < min_vol:
                return
            if sector and sector.lower() not in sec.lower():
                return
            results.append({
                "symbol": t, "market": "US",
                "name": info.get("longName") or info.get("shortName", t),
                "price": info.get("currentPrice") or info.get("regularMarketPrice", 0),
                "change_pct": info.get("regularMarketChangePercent", 0),
                "volume": vol, "market_cap": cap,
                "pe_ratio": pe, "sector": sec,
            })
        except Exception:
            pass

    # Process in batches of 20 to stay under rate limits
    batch_size = 20
    for i in range(0, min(len(tickers), limit * 3), batch_size):
        batch = tickers[i:i + batch_size]
        await asyncio.gather(*[_fetch_one(t) for t in batch])
        if len(results) >= limit:
            break

    return results[:limit]


# ── FRED macro data ───────────────────────────────────────────────

async def get_macro_indicator(name: str) -> list[dict]:
    series_id = SERIES.get(name)
    if not series_id:
        return []
    key = f"us:macro:{name}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)
    data = await get_series(series_id)
    await cache_set(key, json.dumps(data), TTL_HISTORY)
    return data


# ── News ──────────────────────────────────────────────────────────

TTL_NEWS = 5 * 60  # 5 minutes


async def get_news(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    key = f"us:news:{ticker.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    import asyncio
    loop = asyncio.get_event_loop()

    def _fetch():
        import yfinance as yf
        t = yf.Ticker(ticker)
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


async def get_earnings(ticker: str) -> dict[str, Any]:
    """Next earnings date and consensus EPS/revenue estimates from yfinance."""
    key = f"us:earnings:{ticker.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    import asyncio
    loop = asyncio.get_event_loop()

    def _fetch():
        import yfinance as yf
        t = yf.Ticker(ticker)
        try:
            cal = t.calendar
        except Exception:
            cal = None

        if cal is None:
            return {"earnings_date": None, "eps_estimate": None, "revenue_estimate": None}

        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if isinstance(dates, list) and dates:
                raw = dates[0]
                next_date = raw.date().isoformat() if hasattr(raw, "date") else str(raw)
            else:
                next_date = None
            return {
                "earnings_date": next_date,
                "eps_estimate": cal.get("Earnings Average"),
                "revenue_estimate": cal.get("Revenue Average"),
            }
        return {"earnings_date": None, "eps_estimate": None, "revenue_estimate": None}

    result = await loop.run_in_executor(None, _fetch)
    # cache for 6 hours — earnings dates don't change often
    await cache_set(key, json.dumps(result), 6 * 3600)
    return result
