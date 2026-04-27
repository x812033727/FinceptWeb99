"""
Finnhub connector — 4th-tier US quote fallback.

Slot in the waterfall after Polygon / yfinance / Stooq all fail. Free
tier is 60 calls/min, served from US-east infrastructure that isn't
subject to the Yahoo cloud-IP block or Stooq's anti-scrape DNS-overflow
trip. Single-symbol endpoint only on the free plan, but unlike Stooq it
accepts modest concurrency (we cap at 4 in flight to leave headroom).

`get_quote(ticker)` and `get_batch_quotes(tickers)` mirror the existing
stooq_connector signatures so plugging into the service layer is a
drop-in addition. Empty key returns {} — the service waterfall treats
that the same as a hard failure and either continues to the next tier
or surfaces `data_source: "unavailable"`.

The active key is resolved through `services.market_key_service.resolve_key`
which prefers the admin-managed DB row, then the .env fallback. Cached
for 60 s so the screener fan-out (25 in flight) doesn't hammer the DB.

API doc: https://finnhub.io/docs/api/quote
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_TIMEOUT = 8.0
_CONCURRENCY = asyncio.Semaphore(4)


async def _active_key() -> str:
    """DB-managed key takes precedence over the env fallback. Resolved
    inside `market_key_service` with a 60-s cache so this is cheap."""
    from services.market_key_service import resolve_key
    return await resolve_key("finnhub")


def _row_to_quote(row: dict, ticker: str) -> dict[str, Any] | None:
    """Map the Finnhub /quote payload to the shared connector shape.

    Finnhub returns 0 for every field when the symbol is unknown OR when
    the free-tier plan can't access it (e.g. some indices). Treat the
    all-zero response as no-data so the service waterfall continues.
    """
    c = row.get("c")
    if c in (None, 0):
        return None
    pc = row.get("pc") or 0
    change = row.get("d")
    change_pct = row.get("dp")
    if change is None and pc:
        change = round(c - pc, 4)
    if change_pct is None and pc:
        change_pct = round((c - pc) / pc * 100, 4)
    return {
        "symbol": ticker.upper(),
        "price": float(c),
        "change": float(change or 0),
        "change_pct": float(change_pct or 0),
        "volume": 0,   # /quote doesn't carry volume on the free plan
        "open": float(row["o"]) if row.get("o") not in (None, 0) else None,
        "high": float(row["h"]) if row.get("h") not in (None, 0) else None,
        "low":  float(row["l"]) if row.get("l") not in (None, 0) else None,
        "prev_close": float(pc) if pc else None,
        "market_cap": None,
    }


async def _fetch_one(client: httpx.AsyncClient, ticker: str, key: str) -> dict | None:
    async with _CONCURRENCY:
        try:
            r = await client.get(
                _QUOTE_URL,
                params={"symbol": ticker.upper(), "token": key},
            )
            if r.status_code == 429:
                # Free-plan rate-limit hit — back off and let the next tier
                # take over instead of holding the request open.
                log.warning("finnhub.rate_limited", extra={"ticker": ticker})
                return None
            r.raise_for_status()
        except Exception as exc:
            log.warning("finnhub.fetch_failed",
                        extra={"ticker": ticker, "error": str(exc)})
            return None
        try:
            return r.json()
        except ValueError:
            log.warning("finnhub.invalid_json",
                        extra={"ticker": ticker, "body": r.text[:80]})
            return None


async def get_quote(ticker: str) -> dict[str, Any]:
    """Single-symbol quote. Returns {} if Finnhub is disabled, the symbol
    is unknown, or the request fails."""
    key = await _active_key()
    if not key:
        return {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        row = await _fetch_one(c, ticker, key)
    if not row:
        return {}
    quote = _row_to_quote(row, ticker)
    return quote or {}


async def get_batch_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Concurrent (capped at 4 in flight) per-symbol fan-out. ~25 symbols
    finishes in <2 s on a healthy plan. Symbols Finnhub can't price are
    simply absent from the result dict — same convention as Stooq."""
    if not tickers:
        return {}
    key = await _active_key()
    if not key:
        return {}

    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async def _one(ticker: str) -> None:
            row = await _fetch_one(client, ticker, key)
            if not row:
                return
            quote = _row_to_quote(row, ticker)
            if not quote:
                return
            out[ticker] = {
                "price": quote["price"],
                "change_pct": quote["change_pct"],
                "volume": quote["volume"],
            }

        await asyncio.gather(*[_one(t) for t in tickers])
    return out
