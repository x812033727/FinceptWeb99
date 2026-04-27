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
import data.us.stooq_connector as stooq
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

    raw: dict[str, Any] = {}
    try:
        raw = await polygon.get_quote(ticker) if _use_polygon() else await yfinance.get_quote(ticker)
    except Exception:
        try:
            raw = await yfinance.get_quote(ticker)
        except Exception:
            raw = {}

    # Final fallback: when Polygon + yfinance (.info, fast_info, AND
    # yf.download) all yield no price, try Stooq. Stooq is hosted in
    # Europe and isn't subject to the same Yahoo cloud-IP blocks that
    # bite us in some deployments.
    if not raw.get("price"):
        try:
            stooq_raw = await stooq.get_quote(ticker)
            if stooq_raw.get("price"):
                raw = stooq_raw
        except Exception:
            pass

    result = _normalize_quote(ticker, raw)
    # Don't cache the zero-state — keeps the next request retrying instead
    # of locking in a failure for TTL_QUOTE seconds.
    if result.get("price"):
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

    data: list[dict[str, Any]] = []
    if _use_polygon():
        try:
            data = await polygon.get_options_chain(ticker, expiration_date)
        except Exception:
            data = []

    # yfinance fallback covers the no-Polygon path AND the Polygon-failure path.
    # yfinance's option_chain returns less metadata than Polygon's reference
    # endpoint but includes live last_price / IV / OI which Polygon's free
    # tier doesn't, so it's a useful primary source for retail users too.
    if not data:
        try:
            data = await yfinance.get_options(ticker, expiration_date)
        except Exception:
            data = []

    if data:
        await cache_set(key, json.dumps(data), TTL_OPTIONS)
    return data


# ── Screener ──────────────────────────────────────────────────────

# S&P 500 tickers cached in-process (updated daily by scheduler in Phase 5)
async def _get_sp500_tickers() -> list[str]:
    from data.us.sp500_universe import get_sp500_tickers
    return await get_sp500_tickers()


async def get_screener(
    min_market_cap: float | None = None,
    min_pe: float | None = None,
    max_pe: float | None = None,
    min_pb: float | None = None,
    max_pb: float | None = None,
    min_dividend_yield: float | None = None,
    min_volume: int | None = None,
    sector: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    key = (
        f"us:screener:{min_market_cap}:{min_pe}:{max_pe}:{min_pb}:{max_pb}:"
        f"{min_dividend_yield}:{min_volume}:{sector}:{limit}"
    )
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    # Polygon's bulk snapshot endpoint doesn't expose PE/PB/yield/sector,
    # so any fundamental filter forces the yfinance path.
    fundamental_filter = any(
        v is not None for v in (min_pe, max_pe, min_pb, max_pb, min_dividend_yield, sector)
    )

    results: list[dict[str, Any]] = []
    if _use_polygon() and not fundamental_filter:
        try:
            snapshots = await polygon.get_snapshot_all()
        except Exception:
            snapshots = []
        results = _filter_polygon_snapshots(snapshots, min_market_cap, min_volume, limit)

    # Fall through to yfinance when Polygon isn't configured, when a
    # fundamental filter is active (Polygon's snapshot lacks those fields),
    # or when Polygon returned nothing (transient API failure or quota).
    if not results:
        tickers = await _get_sp500_tickers()
        results = await _screener_yfinance(
            tickers,
            min_cap=min_market_cap,
            min_pe=min_pe,
            max_pe=max_pe,
            min_pb=min_pb,
            max_pb=max_pb,
            min_dividend_yield=min_dividend_yield,
            min_vol=min_volume,
            sector=sector,
            limit=limit,
        )

    # Static last-resort fallback: the yfinance .info path was also blocked
    # (Yahoo IP-bans cloud providers fairly often). Emit a curated symbol+
    # name list so the user at least sees a click-through list of US stocks.
    # Try yfinance's chart endpoint first (yf.download), then Stooq's CSV
    # endpoint (hosted in Europe, immune to Yahoo's cloud-IP block) when
    # yfinance is fully unreachable. Skipped when filters were active —
    # we can't honour PE/PB/sector without .info — and when results
    # were already populated.
    if not results and not fundamental_filter and not min_market_cap and not min_volume:
        from data.us.sp500_universe import get_fallback_universe
        universe = get_fallback_universe()[:limit]
        symbols = [sym for sym, _ in universe]
        quotes = await yfinance.get_batch_quotes(symbols)
        if not quotes:
            quotes = await stooq.get_batch_quotes(symbols)
        results = [
            {
                "symbol": sym, "market": "US", "name": name,
                "price": quotes.get(sym, {}).get("price", 0.0),
                "change_pct": quotes.get(sym, {}).get("change_pct", 0.0),
                "volume": quotes.get(sym, {}).get("volume", 0),
                "market_cap": None, "pe_ratio": None,
                "pb_ratio": None, "dividend_yield": None,
                "sector": None,
            }
            for sym, name in universe
        ]

    # Don't cache empty results — keeps the next request retrying instead
    # of locking in a failure for TTL_SCREENER seconds. Same goes for the
    # curated-list fallback when every row has price=0 (batch quotes also
    # blocked) — caching that would lock the user into the zero state.
    if results and any(r.get("price") for r in results):
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


async def _screener_yfinance(
    tickers: list[str],
    *,
    min_cap: float | None = None,
    min_pe: float | None = None,
    max_pe: float | None = None,
    min_pb: float | None = None,
    max_pb: float | None = None,
    min_dividend_yield: float | None = None,
    min_vol: int | None = None,
    sector: str | None = None,
    limit: int = 100,
) -> list[dict]:
    import asyncio
    results = []

    async def _fetch_one(t: str):
        try:
            info = await yfinance.get_info(t)
            cap = info.get("marketCap", 0) or 0
            pe = info.get("trailingPE")
            pb = info.get("priceToBook")
            # yfinance returns dividendYield as a fraction (0.025 = 2.5%);
            # the screener filter and column are both in percent, so multiply.
            raw_yield = info.get("dividendYield")
            yield_pct = (raw_yield * 100) if raw_yield is not None else None
            vol = info.get("volume", 0) or 0
            sec = info.get("sector", "")
            if min_cap and cap < min_cap:
                return
            if min_pe is not None and (pe is None or pe < min_pe):
                return
            if max_pe is not None and (pe is None or pe > max_pe):
                return
            if min_pb is not None and (pb is None or pb < min_pb):
                return
            if max_pb is not None and (pb is None or pb > max_pb):
                return
            if min_dividend_yield is not None and (yield_pct is None or yield_pct < min_dividend_yield):
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
                "pe_ratio": pe,
                "pb_ratio": pb,
                "dividend_yield": round(yield_pct, 3) if yield_pct is not None else None,
                "sector": sec,
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


async def _google_news_rss(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Google News RSS in en-US — same shape as the TW helper."""
    import httpx
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    url = "https://news.google.com/rss/search"
    params = {
        "q":    query,
        "hl":   "en-US",
        "gl":   "US",
        "ceid": "US:en",
    }
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        xml_text = r.text

    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for el in root.findall(".//item")[:limit]:
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        pub_date_raw = el.findtext("pubDate") or ""
        source_el = el.find("source")
        publisher = source_el.text.strip() if (source_el is not None and source_el.text) else ""
        try:
            published_at = parsedate_to_datetime(pub_date_raw).isoformat()
        except (TypeError, ValueError):
            published_at = pub_date_raw
        if not title or not link:
            continue
        items.append({
            "title":        title,
            "publisher":    publisher,
            "link":         link,
            "published_at": published_at,
            "thumbnail":    None,
        })
    return items


async def _yfinance_news_fallback(ticker: str, limit: int) -> list[dict[str, Any]]:
    """Legacy yfinance path. Fragile (Yahoo IP-blocks) but kept as last resort."""
    import asyncio
    loop = asyncio.get_running_loop()

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
                "title":        n.get("title", ""),
                "publisher":    n.get("publisher", ""),
                "link":         n.get("link", ""),
                "published_at": datetime.fromtimestamp(
                    n.get("providerPublishTime", 0), tz=timezone.utc
                ).isoformat(),
                "thumbnail":    thumbnail,
            })
        return items

    return await loop.run_in_executor(None, _fetch)


async def get_news(ticker: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    US news — Google News RSS (en-US) primary, yfinance fallback. Mirrors
    the TW news pipeline. Yahoo blocks many cloud IPs so yfinance.news is
    unreliable in production; Google News covers Reuters / Bloomberg /
    WSJ / MarketWatch / CNBC / Yahoo Finance etc. without an API key.

    Query is `{ticker} {company name}` when a recent quote is cached or
    when the ticker matches the curated fallback universe; otherwise we
    fall back to `{ticker} stock`.
    """
    key = f"us:news:{ticker.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    name = ""
    try:
        q_cached = await cache_get(key_quote("us", ticker))
        if q_cached:
            payload = json.loads(q_cached)
            name = payload.get("name", "") or ""
    except Exception:
        pass
    if not name:
        # Last resort: pull the company name from the curated fallback list.
        from data.us.sp500_universe import get_fallback_universe
        name_map = dict(get_fallback_universe())
        name = name_map.get(ticker.upper(), "")

    query = f"{ticker} {name}".strip() if name else f"{ticker} stock"

    items: list[dict[str, Any]] = []
    try:
        items = await _google_news_rss(query, limit=limit)
    except Exception:
        items = []

    if not items:
        try:
            items = await _yfinance_news_fallback(ticker, limit)
        except Exception:
            items = []

    if items:
        await cache_set(key, json.dumps(items), TTL_NEWS)
    return items


async def get_earnings(ticker: str) -> dict[str, Any]:
    """Next earnings date and consensus EPS/revenue estimates from yfinance."""
    key = f"us:earnings:{ticker.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    import asyncio
    loop = asyncio.get_running_loop()

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
