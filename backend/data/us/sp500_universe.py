"""S&P 500 ticker list — Wikipedia HTML scrape with a static fallback.

The list itself rarely changes (a few rebalances per quarter). We cache the
parsed ticker list in-process and let the daily APScheduler job refresh it
by calling `fetch_sp500_tickers(force_refresh=True)`. On-demand callers
(e.g. `services/us_market_service.py::get_screener`) call `get_sp500_tickers()`
which only hits the network on cold start.

If Wikipedia is unreachable (network policy, geo block, HTML structure
change), we fall back to a curated list of the largest / most-traded US
names so search and the market list never collapse to zero results.
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

# Curated fallback — top ~80 US names by market cap / liquidity. Used when
# the Wikipedia scrape fails or returns no matches. Order is roughly
# market-cap descending so results stay sensible even before a real refresh.
_FALLBACK_TICKERS: list[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "AVGO", "TSLA", "ORCL",
    # Financials
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK",
    # Healthcare
    "LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "TMO", "ABT", "AMGN", "DHR",
    # Consumer / retail
    "WMT", "PG", "HD", "COST", "KO", "PEP", "MCD", "NKE", "SBUX", "TGT",
    # Industrial / energy
    "XOM", "CVX", "GE", "CAT", "BA", "RTX", "HON", "UNP", "LMT", "DE",
    # Comm services / media
    "NFLX", "DIS", "TMUS", "VZ", "T", "CMCSA",
    # Semis / hardware
    "AMD", "QCOM", "INTC", "TXN", "MU", "AMAT", "LRCX", "ADI",
    # Software / cloud
    "CRM", "ADBE", "NOW", "INTU", "CSCO", "IBM", "PLTR",
    # Utilities / staples / misc
    "NEE", "DUK", "SO", "MO", "PM", "PYPL", "SQ", "UBER", "ABNB", "BKNG",
    # Index ETFs commonly searched
    "SPY", "QQQ", "DIA", "IWM", "VTI", "VOO",
]


def _dedupe(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


_cache: list[str] = []


async def _fetch_from_wikipedia() -> list[str]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as c:
        r = await c.get(_URL)
    raw = _TICKER_RE.findall(r.text)
    return _dedupe(raw)[:_MAX_TICKERS]


async def get_sp500_tickers(force_refresh: bool = False) -> list[str]:
    """Return cached S&P 500 tickers; fetch from Wikipedia on miss or refresh.

    Failures fall back to a static curated list so search and the market
    list always have *something* to render. The fallback is also returned
    when Wikipedia responds but the regex matches nothing (HTML changed) —
    we treat empty as "fetch failed" rather than caching it.
    """
    global _cache
    if _cache and not force_refresh:
        return _cache
    try:
        fetched = await _fetch_from_wikipedia()
    except Exception as exc:
        log.warning("sp500.fetch_failed", extra={"error": str(exc)})
        fetched = []

    if fetched:
        _cache = fetched
    elif not _cache:
        # Cold start with a failed fetch — populate from fallback so callers
        # see the curated list. Don't overwrite a successful prior cache.
        log.warning("sp500.using_fallback")
        _cache = list(_FALLBACK_TICKERS)
    return _cache
