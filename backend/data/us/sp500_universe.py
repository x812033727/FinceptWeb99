"""S&P 500 ticker list — Wikipedia HTML scrape.

The list itself rarely changes (a few rebalances per quarter). We cache the
parsed ticker list in-process and let the daily APScheduler job refresh it
by calling `fetch_sp500_tickers(force_refresh=True)`. On-demand callers
(e.g. `services/us_market_service.py::get_screener`) call `get_sp500_tickers()`
which only hits the network on cold start.

Both code paths share a single module-level cache so the scheduler's daily
refresh is actually visible to request handlers (the previous design had two
independent caches, so the scheduler did work nobody read).
"""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_TICKER_RE = re.compile(r'<td><a[^>]+>([A-Z]{1,5})</a></td>')
_TIMEOUT_S = 15.0
_MAX_TICKERS = 505

_cache: list[str] = []


async def _fetch_from_wikipedia() -> list[str]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as c:
        r = await c.get(_URL)
    raw = _TICKER_RE.findall(r.text)
    return list(dict.fromkeys(raw))[:_MAX_TICKERS]


async def get_sp500_tickers(force_refresh: bool = False) -> list[str]:
    """Return cached S&P 500 tickers; fetch from Wikipedia on miss or refresh.

    Failures are swallowed and the existing cache (possibly empty) is returned
    so callers don't need to handle network errors — same contract as the
    other US connectors.
    """
    global _cache
    if _cache and not force_refresh:
        return _cache
    try:
        _cache = await _fetch_from_wikipedia()
    except Exception as exc:
        log.warning("sp500.fetch_failed", extra={"error": str(exc)})
    return _cache
