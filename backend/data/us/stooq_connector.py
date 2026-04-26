"""
Stooq CSV connector — free, no API key, runs from a Polish edge that
isn't subject to the same Yahoo rate-limit / IP-block we hit on cloud
providers. Used as the final-tier fallback for US quotes / screener
when both Polygon and yfinance return nothing.

Stooq's instant-quote endpoint (`q/l/`) returns one CSV row per call.
Batch via comma-separated symbols hits a "DNS cache overflow" anti-
scrape response, and the history endpoint (`q/d/l/`) is captcha-gated
behind an apikey, so the only usable shape is single-symbol calls.
Stooq's rate limiter also rejects concurrent fan-out (even 2 in flight
trips a 503), so batch fetches walk the symbol list sequentially with
a small inter-request delay.

The `f=sd2t2ohlcvp` field set returns Symbol/Date/Time/Open/High/Low/
Close/Volume/Prev — `Prev` is the previous session close, which gives
us change% without needing a separate history call.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_INSTANT_URL = "https://stooq.com/q/l/"
_TIMEOUT = 10.0
_FIELDS = "sd2t2ohlcvp"
# Empirically, Stooq accepts ~5 req/sec from a single client. Concurrent
# requests (even 2 in flight) trip a 503 DNS-overflow response. 0.2s
# between calls clears that with margin.
_INTER_REQUEST_DELAY_S = 0.2


def _to_stooq(ticker: str) -> str:
    """Convert ticker to Stooq's lowercase `<symbol>.us` form. BRK-B etc.
    keep their dash; Stooq accepts it."""
    return f"{ticker.lower()}.us"


def _safe_float(s: str | None) -> float | None:
    if s is None or s in ("", "N/D"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _safe_int(s: str | None) -> int | None:
    f = _safe_float(s)
    return int(f) if f is not None else None


async def _fetch_one(client: httpx.AsyncClient, ticker: str) -> dict | None:
    """Fetch one symbol's CSV row. Returns the parsed dict or None on error /
    no data. Caller is responsible for spacing requests; see _INTER_REQUEST_DELAY_S."""
    try:
        r = await client.get(
            _INSTANT_URL,
            params={"s": _to_stooq(ticker), "i": "d", "f": _FIELDS, "h": "", "e": "csv"},
        )
        r.raise_for_status()
    except Exception as exc:
        log.warning("stooq.fetch_failed", extra={"ticker": ticker, "error": str(exc)})
        return None
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return rows[0] if rows else None


def _row_to_quote(row: dict) -> dict[str, Any] | None:
    close = _safe_float(row.get("Close"))
    if close is None:
        return None
    prev = _safe_float(row.get("Prev"))
    change = round(close - prev, 4) if prev else 0
    change_pct = round(change / prev * 100, 4) if prev else 0
    return {
        "price": close,
        "change": change,
        "change_pct": change_pct,
        "volume": _safe_int(row.get("Volume")) or 0,
        "open": _safe_float(row.get("Open")),
        "high": _safe_float(row.get("High")),
        "low": _safe_float(row.get("Low")),
        "prev_close": prev,
    }


async def get_quote(ticker: str) -> dict[str, Any]:
    """Single-symbol quote with change%. Returns {} if Stooq has no data."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        row = await _fetch_one(c, ticker)
    if row is None:
        return {}
    q = _row_to_quote(row)
    if q is None:
        return {}
    return {
        "symbol": ticker.upper(),
        **q,
        "market_cap": None,
    }


async def get_batch_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Batch quote: walk the symbol list sequentially with a small delay
    between requests. Stooq rejects parallel calls (DNS-overflow 503) and
    its comma-separated batch endpoint is also broken, so this is the
    only path that works. ~5 syms/sec → ~6s for the curated 30-name
    universe; result is cached for TTL_SCREENER (10 min) at the service
    layer. Missing tickers are simply absent from the result."""
    if not tickers:
        return {}

    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for i, ticker in enumerate(tickers):
            if i > 0:
                await asyncio.sleep(_INTER_REQUEST_DELAY_S)
            row = await _fetch_one(c, ticker)
            if row is None:
                continue
            q = _row_to_quote(row)
            if q is None:
                continue
            out[ticker] = {
                "price": q["price"],
                "change_pct": q["change_pct"],
                "volume": q["volume"],
            }
    return out
