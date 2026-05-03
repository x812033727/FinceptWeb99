"""
US market service — owns all caching and waterfall fallback logic.
Rules:
  - Always check Redis first
  - Polygon.io if API key is set, else yfinance
  - All timestamps returned as Unix ms UTC
  - Market-hours helper uses America/New_York tz
"""
import asyncio
import json
import logging
from datetime import datetime, timezone, date, timedelta
from typing import Any
import pytz

from cache.redis_cache import cache_get, cache_set, key_quote, key_history, key_fundamentals
from config import settings
from services._quote_helpers import sanitize_change_pct
import data.us.finnhub_connector as finnhub
import data.us.polygon_connector as polygon
import data.us.stooq_connector as stooq
import data.us.yfinance_connector as yfinance
from data.us.fred_connector import get_series, SERIES

log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")

# ── TTLs (seconds) ────────────────────────────────────────────────
# TTLs centralized in `cache.cache_ttls`. Aliases preserve in-module
# call-site shape.
from cache.cache_ttls import (  # noqa: E402
    TTL_EARNINGS,
    TTL_FUNDAMENTALS,
    TTL_HISTORY_DAILY as TTL_HISTORY,
    TTL_OPTIONS,
    TTL_QUOTE_US as TTL_QUOTE,
    TTL_SCREENER,
)

# Stooq's per-request 0.2s pacing means a batch of 100 symbols takes
# ~20 seconds — longer than any sane reverse-proxy timeout. We cap the
# synchronous request path to STOOQ_SYNC_BATCH_LIMIT symbols (~5 s
# worst case) and let the rest come back as price=0 placeholders that
# the frontend renders as "—". The background warm task
# (`tasks.us_market_refresh.refresh_us_screener`) refreshes the cache
# every 5 min with the FULL universe so subsequent users hit the cache.
STOOQ_SYNC_BATCH_LIMIT = 25

# yfinance ships a single ThreadPoolExecutor with 8 worker threads
# (data/us/yfinance_connector.py::_MAX_WORKERS). When _screener_yfinance
# fans out 20 concurrent `.info` calls per batch they all submit to that
# same pool, so an ad-hoc `/quote/AAPL` request lands behind the screener
# queue and blocks for seconds. Cap the screener fan-out at 4 in flight
# so half the yfinance pool stays free for live quote requests.
_SCREENER_INFO_CONCURRENCY = asyncio.Semaphore(4)


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

async def fetch_quote_waterfall(ticker: str) -> tuple[dict[str, Any], str]:
    """Run the Polygon → yfinance → Stooq waterfall and return (raw, source).

    Pulled out of get_quote() so the background WS-publish task can reuse
    the same fallback chain. Without this, the polling task only tried
    the primary source and gave up — meaning a Polygon rate-limit pause
    froze every subscriber's live price for the whole window.
    """
    raw: dict[str, Any] = {}
    source = "unavailable"
    primary = "polygon" if _use_polygon() else "yfinance"
    try:
        raw = await polygon.get_quote(ticker) if _use_polygon() else await yfinance.get_quote(ticker)
        if raw.get("price"):
            source = primary
    except Exception as exc:
        log.warning("us.quote.primary_failed",
                    extra={"ticker": ticker, "source": primary, "error": str(exc)})
        try:
            raw = await yfinance.get_quote(ticker)
            if raw.get("price"):
                source = "yfinance"
        except Exception as exc2:
            log.warning("us.quote.yfinance_fallback_failed",
                        extra={"ticker": ticker, "error": str(exc2)})
            raw = {}

    # Tier 3 fallback: when Polygon + yfinance (.info, fast_info, AND
    # yf.download) all yield no price, try Stooq. Stooq is hosted in
    # Europe and isn't subject to the same Yahoo cloud-IP blocks that
    # bite us in some deployments.
    if not raw.get("price"):
        try:
            stooq_raw = await stooq.get_quote(ticker)
            if stooq_raw.get("price"):
                raw = stooq_raw
                source = "stooq"
        except Exception as exc:
            log.warning("us.quote.stooq_fallback_failed",
                        extra={"ticker": ticker, "error": str(exc)})

    # Tier 4 fallback: Finnhub. Different infra from both Yahoo (yfinance)
    # and the Polish edge (Stooq), so when the upstream cluster blocks
    # those two simultaneously this still pulls a real price. Free-tier
    # 60/min is plenty for an ad-hoc per-symbol call. Skipped silently
    # when FINNHUB_API_KEY is empty so deployments that haven't opted in
    # behave identically to the previous 3-tier waterfall.
    if not raw.get("price"):
        try:
            finnhub_raw = await finnhub.get_quote(ticker)
            if finnhub_raw.get("price"):
                raw = finnhub_raw
                source = "finnhub"
        except Exception as exc:
            log.warning("us.quote.finnhub_fallback_failed",
                        extra={"ticker": ticker, "error": str(exc)})

    if not raw.get("price"):
        log.warning("us.quote.all_sources_failed", extra={"ticker": ticker})

    return raw, source


async def get_quote(
    ticker: str, *, bypass_cache: bool = False,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Read US quote. Hits Redis cache first unless `bypass_cache` is
    set — caller forces a fresh upstream fetch when the user opens a
    view that promises live prices (e.g. watchlist page on mount).

    `as_of` (PR #226): backtest mode. When set, bypasses live waterfall
    and reads close/prev_close from `ohlcv_daily` at the most recent bar
    with `ts <= as_of`. Empty dict when no archived bar in range."""
    if as_of is not None:
        from services.ingest.repository import read_ohlcv_range_autosession
        bars = await read_ohlcv_range_autosession(
            "US", ticker.upper(), as_of - timedelta(days=10), as_of,
        )
        if not bars:
            return {}
        last = bars[-1]
        prev = bars[-2]["close"] if len(bars) >= 2 else None
        result = _normalize_quote(ticker, {
            "price":      last.get("close"),
            "open":       last.get("open"),
            "high":       last.get("high"),
            "low":        last.get("low"),
            "volume":     last.get("volume", 0),
            "prev_close": prev,
            "change":     (
                last.get("close") - prev
                if (last.get("close") is not None and prev is not None)
                else None
            ),
            "change_pct": (
                round((last["close"] - prev) / prev * 100, 4)
                if (last.get("close") is not None and prev) else None
            ),
        })
        result["data_source"] = "ohlcv_daily"
        result["as_of"] = last["time"]
        return result

    key = key_quote("us", ticker)
    if not bypass_cache:
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)

    raw, source = await fetch_quote_waterfall(ticker)
    result = _normalize_quote(ticker, raw)
    result["data_source"] = source
    # Don't cache the zero-state — keeps the next request retrying instead
    # of locking in a failure for TTL_QUOTE seconds.
    if result.get("price"):
        await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


def _normalize_quote(ticker: str, raw: dict) -> dict[str, Any]:
    # ±30% sanity bound on `change_pct` — Polygon / yfinance / Stooq
    # / Finnhub very occasionally surface implausible deltas
    # (split-adjusted vs un-adjusted historicals, snapshot rows
    # written before backfill). Same rail TW uses for the KY-listed
    # +992% class of bug. Drops `change` alongside so the screener
    # row stays consistent.
    chg_pct = sanitize_change_pct(
        ticker, "US", raw.get("change_pct"),
    )
    chg = raw.get("change") if chg_pct is not None else None
    return {
        "symbol": ticker.upper(),
        "market": "US",
        "name": raw.get("name", ticker.upper()),
        "price": raw.get("price", 0),
        "change": chg if chg is not None else 0,
        "change_pct": chg_pct if chg_pct is not None else 0,
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
    key = key_history("us", ticker, interval, range_token=period)
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
    except Exception as exc:
        log.warning("us.history.primary_failed",
                    extra={"ticker": ticker, "period": period, "interval": interval, "error": str(exc)})
        try:
            bars = await yfinance.get_history(ticker, period=period, interval=interval)
        except Exception as exc2:
            log.warning("us.history.yfinance_fallback_failed",
                        extra={"ticker": ticker, "error": str(exc2)})
            bars = []

    # Normalize time to "YYYY-MM-DD" string for daily, Unix ms for intraday
    result = _normalize_bars(bars, interval)
    # Skip cache when empty — avoids locking in 4h of nothing on transient
    # upstream failure (matches the get_quote pattern).
    if result:
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

    info: dict[str, Any] = {}
    source = "unavailable"
    try:
        if _use_polygon():
            details = await polygon.get_ticker_details(ticker)
            result = _normalize_fundamentals_polygon(ticker, details)
            source = "polygon"
        else:
            raise RuntimeError("no polygon key")
    except Exception as exc:
        if _use_polygon():
            log.warning("us.fundamentals.polygon_failed",
                        extra={"ticker": ticker, "error": str(exc)})
        try:
            info = await yfinance.get_info(ticker)
        except Exception as exc2:
            log.warning("us.fundamentals.yfinance_failed",
                        extra={"ticker": ticker, "error": str(exc2)})
            info = {}
        result = _normalize_fundamentals_yf(ticker, info)
        if info:
            source = "yfinance"

    # Don't cache when we got nothing useful from yfinance either —
    # otherwise the user is locked into 24h of empty fundamentals.
    has_payload = bool(info) or result.get("market_cap") or result.get("pe_ratio")
    result["data_source"] = source
    if has_payload:
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
            raise RuntimeError("no polygon key")
    except Exception as exc:
        if _use_polygon():
            log.warning("us.financials.polygon_failed",
                        extra={"ticker": ticker, "error": str(exc)})
        try:
            data = await yfinance.get_financials(ticker)
        except Exception as exc2:
            log.warning("us.financials.yfinance_failed",
                        extra={"ticker": ticker, "error": str(exc2)})
            data = {"income_statement": [], "balance_sheet": [], "cash_flow": []}
        result = {"source": "yfinance", **data}

    payload = result.get("data") or any(
        result.get(k) for k in ("income_statement", "balance_sheet", "cash_flow")
    )
    if payload:
        await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


# ── Options chain ─────────────────────────────────────────────────

async def get_options(ticker: str, expiration_date: str | None = None) -> list[dict[str, Any]]:
    key = f"us:options:{ticker}:{expiration_date or 'all'}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    data: list[dict[str, Any]] = []
    source = "unavailable"
    if _use_polygon():
        try:
            data = await polygon.get_options_chain(ticker, expiration_date)
            if data:
                source = "polygon"
        except Exception as exc:
            log.warning("us.options.polygon_failed",
                        extra={"ticker": ticker, "expiry": expiration_date, "error": str(exc)})
            data = []

    # yfinance fallback covers the no-Polygon path AND the Polygon-failure path.
    # yfinance's option_chain returns less metadata than Polygon's reference
    # endpoint but includes live last_price / IV / OI which Polygon's free
    # tier doesn't, so it's a useful primary source for retail users too.
    if not data:
        try:
            data = await yfinance.get_options(ticker, expiration_date)
            if data:
                source = "yfinance"
        except Exception as exc:
            log.warning("us.options.yfinance_failed",
                        extra={"ticker": ticker, "expiry": expiration_date, "error": str(exc)})
            data = []

    # Stamp every contract row so the frontend can render a free-tier hint
    # (data_source == "yfinance") or flag a Polygon-failure-degradation.
    # setdefault keeps any per-row source the connector already set.
    for row in data:
        row.setdefault("data_source", source)

    if data:
        await cache_set(key, json.dumps(data), TTL_OPTIONS)
    else:
        log.info("us.options.empty", extra={"ticker": ticker, "expiry": expiration_date})
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
    *,
    full_stooq_batch: bool = False,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """
    `as_of` (PR #226): backtest mode. Reads `ohlcv_daily` at the
    snapshot date instead of the live Polygon / yfinance / Stooq /
    Finnhub waterfall. Fundamental filters (PE / PB / yield / sector /
    market_cap) are skipped — the per-day archive doesn't carry them.
    """
    if as_of is not None:
        return await _get_screener_backtest(
            as_of=as_of,
            min_volume=min_volume,
            limit=limit,
        )

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
        except Exception as exc:
            log.warning("us.screener.polygon_snapshot_failed", extra={"error": str(exc)})
            snapshots = []
        results = _filter_polygon_snapshots(snapshots, min_market_cap, min_volume, limit)

    # Fall through to yfinance when Polygon isn't configured, when a
    # fundamental filter is active (Polygon's snapshot lacks those fields),
    # or when Polygon returned nothing (transient API failure or quota).
    #
    # `_screener_yfinance` walks the S&P 500 list and calls `.info` per
    # symbol — Yahoo IP-bans cloud providers there fairly often, and a
    # blocked .info call can hang for 30 s+. With 100 symbols that easily
    # blows past any reverse-proxy timeout, so we ONLY take that path when
    # we genuinely need the per-symbol fundamental fields it returns.
    # When there are no fundamental filters and Polygon isn't available,
    # skip straight to the universe fallback below — same shape, no .info.
    needs_fundamental_data = (
        fundamental_filter or min_market_cap is not None or min_volume is not None
    )
    if not results and (_use_polygon() or needs_fundamental_data):
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
    # yfinance is fully unreachable.
    #
    # Skipped only when fundamental filters were active — we can't honour
    # PE / PB / sector / dividend yield without .info. min_market_cap is
    # dropped here (batch quotes don't carry market cap) and min_volume is
    # applied post-hoc since batch quotes do include daily volume — better
    # to return a price-only universe than a blank page.
    if not results and not fundamental_filter:
        from data.us.sp500_universe import get_fallback_universe
        universe = get_fallback_universe()[:limit]
        symbols = [sym for sym, _ in universe]
        batch_source = "yfinance"
        quotes = await yfinance.get_batch_quotes(symbols)
        if not quotes:
            log.warning("us.screener.batch_yfinance_empty",
                        extra={"symbol_count": len(symbols)})
            # Cap the Stooq sync batch unless the caller is the background
            # warm task (which can afford to wait the full ~20s).
            stooq_targets = (
                symbols if full_stooq_batch else symbols[:STOOQ_SYNC_BATCH_LIMIT]
            )
            quotes = await stooq.get_batch_quotes(stooq_targets)
            batch_source = "stooq"
            if not quotes:
                log.warning("us.screener.batch_stooq_empty",
                            extra={"symbol_count": len(stooq_targets)})
                # Final tier: Finnhub. Only triggers when both yfinance
                # and Stooq returned nothing — same fan-out cap (25 in
                # the sync path) so a 60/min free quota isn't blown
                # in one request, and the background warm task using
                # full_stooq_batch=True naturally sees the same fall-
                # through with the full universe.
                finnhub_targets = (
                    symbols if full_stooq_batch else symbols[:STOOQ_SYNC_BATCH_LIMIT]
                )
                quotes = await finnhub.get_batch_quotes(finnhub_targets)
                batch_source = "finnhub"
                if not quotes:
                    log.warning("us.screener.batch_finnhub_empty",
                                extra={"symbol_count": len(finnhub_targets)})
        if min_market_cap is not None:
            log.info("us.screener.min_market_cap_dropped",
                     extra={"min_market_cap": min_market_cap,
                            "reason": "batch fallback has no market cap"})
        rows = []
        for sym, name in universe:
            quote = quotes.get(sym)
            rows.append({
                "symbol": sym, "market": "US", "name": name,
                "price": (quote or {}).get("price", 0.0),
                "change_pct": (quote or {}).get("change_pct", 0.0),
                "volume": (quote or {}).get("volume", 0),
                "market_cap": None, "pe_ratio": None,
                "pb_ratio": None, "dividend_yield": None,
                "sector": None,
                # Symbols that the batch endpoint actually returned data for
                # carry the upstream source; the rest are placeholder shells
                # the page renders so the user gets a click-through list.
                "data_source": batch_source if quote else "unavailable",
            })
        if min_volume:
            rows = [r for r in rows if (r["volume"] or 0) >= min_volume]
        results = rows

    # Cache strategy:
    #   - rows with at least one real price ⇒ full TTL_SCREENER (10 min)
    #   - rows with all prices = 0 (every quote provider blocked)
    #     ⇒ short TTL so the page renders something fast but the next
    #       background warm task still gets to refresh quickly.
    #   - empty results ⇒ never cache.
    if results:
        ttl = TTL_SCREENER if any(r.get("price") for r in results) else 60
        await cache_set(key, json.dumps(results), ttl)
    return results


async def _get_screener_backtest(
    *, as_of: date, min_volume: int | None, limit: int,
) -> list[dict[str, Any]]:
    """Backtest mode for `get_screener`. Reads US bars from `ohlcv_daily`
    over a 10-day window ending at `as_of`, computes change_pct from
    the latest bar vs the prior bar per symbol. No fundamental fields
    (yield/PE/PB/market_cap) — archive doesn't carry them.
    """
    from db.session import AsyncSessionLocal
    from models.ohlcv_daily import OhlcvDaily
    from sqlalchemy import select

    start = as_of - timedelta(days=10)
    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                select(OhlcvDaily)
                .where(
                    OhlcvDaily.market == "US",
                    OhlcvDaily.ts >= start,
                    OhlcvDaily.ts <= as_of,
                )
                .order_by(OhlcvDaily.symbol, OhlcvDaily.ts)
            )
            rows = list((await db.scalars(stmt)).all())
    except Exception as exc:
        log.warning("us.screener.backtest_db_error",
                    extra={"as_of": as_of.isoformat(), "error": str(exc)})
        return []

    by_symbol: dict[str, list[Any]] = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)

    out: list[dict[str, Any]] = []
    for symbol, bars in by_symbol.items():
        last = bars[-1]
        close = float(last.close) if last.close is not None else None
        vol = int(last.volume or 0)
        if min_volume and vol < min_volume:
            continue
        prev = (
            float(bars[-2].close)
            if len(bars) >= 2 and bars[-2].close is not None
            else None
        )
        change = (close - prev) if (close is not None and prev is not None) else None
        change_pct = (
            round(change / prev * 100, 4)
            if (change is not None and prev) else None
        )
        change_pct = sanitize_change_pct(symbol, "US", change_pct)
        if change_pct is None:
            change = None
        out.append({
            "symbol":      symbol,
            "market":      "US",
            "name":        symbol,
            "price":       close,
            "change":      change,
            "change_pct":  change_pct,
            "volume":      vol,
            "open":        float(last.open) if last.open is not None else None,
            "high":        float(last.high) if last.high is not None else None,
            "low":         float(last.low)  if last.low  is not None else None,
            "prev_close":  prev,
            "data_source": "ohlcv_daily",
            "as_of":       last.ts.isoformat(),
        })

    out.sort(key=lambda r: r["volume"] or 0, reverse=True)
    return out[:limit]


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
            "data_source": "polygon",
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
    results = []

    async def _fetch_one(t: str):
        # See _SCREENER_INFO_CONCURRENCY comment: keep at most 4 .info
        # calls in flight at once so half the yfinance ThreadPool stays
        # free for ad-hoc /quote requests. The outer batch=20 still caps
        # how many tickers we process per gather() round.
        async with _SCREENER_INFO_CONCURRENCY:
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
                    "data_source": "yfinance",
                })
            except Exception as exc:
                log.warning("us.screener.yfinance_info_failed",
                            extra={"ticker": t, "error": str(exc)})

    # Process in batches of 20 to stay under rate limits
    batch_size = 20
    for i in range(0, min(len(tickers), limit * 3), batch_size):
        batch = tickers[i:i + batch_size]
        await asyncio.gather(*[_fetch_one(t) for t in batch])
        if len(results) >= limit:
            break

    return results[:limit]


# ── FRED macro data ───────────────────────────────────────────────

async def get_macro_indicator(
    name: str, *, as_of: date | None = None,
) -> list[dict]:
    """Fetch one FRED macro series (configured in `SERIES`).

    `as_of` (PR #226): backtest mode. Passes `observation_end=as_of`
    to the FRED connector so the series doesn't include data points
    after the backtest anchor. Cache key is per-as_of so historical
    reads don't poison the live cache and vice versa."""
    series_id = SERIES.get(name)
    if not series_id:
        return []
    if as_of is not None:
        key = f"us:macro:{name}:asof={as_of.isoformat()}"
    else:
        key = f"us:macro:{name}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)
    data = await get_series(
        series_id,
        end_date=as_of.isoformat() if as_of is not None else None,
    )
    # Don't cache an empty series for 4h — without this, a single FRED
    # outage (or a series with no yfinance fallback like CPI/GDP/UNRATE
    # while the FRED key is unset) locks the user out of macro data for
    # the rest of the TTL window.
    if data:
        await cache_set(key, json.dumps(data), TTL_HISTORY)
    else:
        log.warning("us.macro.empty_series", extra={"name": name, "series_id": series_id})
    return data


# ── News ──────────────────────────────────────────────────────────

from cache.cache_ttls import TTL_NEWS  # noqa: E402


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
    await cache_set(key, json.dumps(result), TTL_EARNINGS)
    return result
