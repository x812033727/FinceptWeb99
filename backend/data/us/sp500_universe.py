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
# the Wikipedia scrape fails or returns no matches. (symbol, company name)
# tuples so the screener can emit a click-through list even when yfinance
# is also unreachable (Yahoo IP-blocks cloud providers fairly often).
_FALLBACK_UNIVERSE: list[tuple[str, str]] = [
    # Mega-cap tech
    ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"),
    ("NVDA", "NVIDIA Corporation"), ("GOOGL", "Alphabet Inc. Class A"),
    ("GOOG", "Alphabet Inc. Class C"), ("AMZN", "Amazon.com Inc."),
    ("META", "Meta Platforms Inc."), ("AVGO", "Broadcom Inc."),
    ("TSLA", "Tesla Inc."), ("ORCL", "Oracle Corporation"),
    # Financials
    ("BRK-B", "Berkshire Hathaway Inc. Class B"), ("JPM", "JPMorgan Chase & Co."),
    ("V", "Visa Inc."), ("MA", "Mastercard Incorporated"),
    ("BAC", "Bank of America Corp."), ("WFC", "Wells Fargo & Company"),
    ("GS", "Goldman Sachs Group Inc."), ("MS", "Morgan Stanley"),
    ("AXP", "American Express Company"), ("BLK", "BlackRock Inc."),
    # Healthcare
    ("LLY", "Eli Lilly and Company"), ("UNH", "UnitedHealth Group"),
    ("JNJ", "Johnson & Johnson"), ("MRK", "Merck & Co."),
    ("ABBV", "AbbVie Inc."), ("PFE", "Pfizer Inc."),
    ("TMO", "Thermo Fisher Scientific"), ("ABT", "Abbott Laboratories"),
    ("AMGN", "Amgen Inc."), ("DHR", "Danaher Corporation"),
    # Consumer / retail
    ("WMT", "Walmart Inc."), ("PG", "Procter & Gamble"),
    ("HD", "Home Depot Inc."), ("COST", "Costco Wholesale"),
    ("KO", "Coca-Cola Company"), ("PEP", "PepsiCo Inc."),
    ("MCD", "McDonald's Corporation"), ("NKE", "Nike Inc."),
    ("SBUX", "Starbucks Corporation"), ("TGT", "Target Corporation"),
    # Industrial / energy
    ("XOM", "Exxon Mobil Corporation"), ("CVX", "Chevron Corporation"),
    ("GE", "General Electric Company"), ("CAT", "Caterpillar Inc."),
    ("BA", "Boeing Company"), ("RTX", "RTX Corporation"),
    ("HON", "Honeywell International"), ("UNP", "Union Pacific Corporation"),
    ("LMT", "Lockheed Martin"), ("DE", "Deere & Company"),
    # Comm services / media
    ("NFLX", "Netflix Inc."), ("DIS", "Walt Disney Company"),
    ("TMUS", "T-Mobile US"), ("VZ", "Verizon Communications"),
    ("T", "AT&T Inc."), ("CMCSA", "Comcast Corporation"),
    # Semis / hardware
    ("AMD", "Advanced Micro Devices"), ("QCOM", "Qualcomm Inc."),
    ("INTC", "Intel Corporation"), ("TXN", "Texas Instruments"),
    ("MU", "Micron Technology"), ("AMAT", "Applied Materials"),
    ("LRCX", "Lam Research"), ("ADI", "Analog Devices"),
    # Software / cloud
    ("CRM", "Salesforce Inc."), ("ADBE", "Adobe Inc."),
    ("NOW", "ServiceNow Inc."), ("INTU", "Intuit Inc."),
    ("CSCO", "Cisco Systems"), ("IBM", "International Business Machines"),
    ("PLTR", "Palantir Technologies"),
    # Utilities / staples / misc
    ("NEE", "NextEra Energy"), ("DUK", "Duke Energy"),
    ("SO", "Southern Company"), ("MO", "Altria Group"),
    ("PM", "Philip Morris International"), ("PYPL", "PayPal Holdings"),
    ("SQ", "Block Inc."), ("UBER", "Uber Technologies"),
    ("ABNB", "Airbnb Inc."), ("BKNG", "Booking Holdings"),
    # Index ETFs commonly searched
    ("SPY", "SPDR S&P 500 ETF"), ("QQQ", "Invesco QQQ Trust"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("VTI", "Vanguard Total Stock Market ETF"),
    ("VOO", "Vanguard S&P 500 ETF"),
]

# Symbols-only view kept for backwards compat with callers that just need
# the ticker list (search endpoint, screener iteration).
_FALLBACK_TICKERS: list[str] = [sym for sym, _ in _FALLBACK_UNIVERSE]


def get_fallback_universe() -> list[tuple[str, str]]:
    """Curated (symbol, name) pairs for click-through lists when yfinance
    is unreachable. Same ordering as _FALLBACK_TICKERS."""
    return list(_FALLBACK_UNIVERSE)


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
