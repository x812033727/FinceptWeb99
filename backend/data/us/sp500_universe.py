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
    # ── Mega-cap tech ────────────────────────────────────────────
    ("AAPL", "Apple Inc."), ("MSFT", "Microsoft Corporation"),
    ("NVDA", "NVIDIA Corporation"), ("GOOGL", "Alphabet Inc. Class A"),
    ("GOOG", "Alphabet Inc. Class C"), ("AMZN", "Amazon.com Inc."),
    ("META", "Meta Platforms Inc."), ("AVGO", "Broadcom Inc."),
    ("TSLA", "Tesla Inc."), ("ORCL", "Oracle Corporation"),
    # ── Financials — banks / cards / insurers / asset mgr ────────
    ("BRK-B", "Berkshire Hathaway Inc. Class B"), ("JPM", "JPMorgan Chase & Co."),
    ("V", "Visa Inc."), ("MA", "Mastercard Incorporated"),
    ("BAC", "Bank of America Corp."), ("WFC", "Wells Fargo & Company"),
    ("GS", "Goldman Sachs Group Inc."), ("MS", "Morgan Stanley"),
    ("AXP", "American Express Company"), ("BLK", "BlackRock Inc."),
    ("C", "Citigroup Inc."), ("USB", "U.S. Bancorp"),
    ("PNC", "PNC Financial Services"), ("TFC", "Truist Financial"),
    ("SCHW", "Charles Schwab Corporation"), ("BK", "Bank of New York Mellon"),
    ("AIG", "American International Group"), ("MET", "MetLife Inc."),
    ("PRU", "Prudential Financial"), ("ALL", "Allstate Corporation"),
    ("TRV", "Travelers Companies"), ("CB", "Chubb Limited"),
    ("PGR", "Progressive Corporation"), ("MMC", "Marsh & McLennan"),
    ("ICE", "Intercontinental Exchange"), ("CME", "CME Group"),
    ("SPGI", "S&P Global"), ("MCO", "Moody's Corporation"),
    ("COF", "Capital One Financial"), ("DFS", "Discover Financial Services"),
    ("FIS", "Fidelity National Information Services"),
    # ── Healthcare — pharma, devices, payors, services ───────────
    ("LLY", "Eli Lilly and Company"), ("UNH", "UnitedHealth Group"),
    ("JNJ", "Johnson & Johnson"), ("MRK", "Merck & Co."),
    ("ABBV", "AbbVie Inc."), ("PFE", "Pfizer Inc."),
    ("TMO", "Thermo Fisher Scientific"), ("ABT", "Abbott Laboratories"),
    ("AMGN", "Amgen Inc."), ("DHR", "Danaher Corporation"),
    ("BMY", "Bristol-Myers Squibb"), ("GILD", "Gilead Sciences"),
    ("CVS", "CVS Health Corporation"), ("CI", "Cigna Group"),
    ("ELV", "Elevance Health"), ("HUM", "Humana Inc."),
    ("MDT", "Medtronic plc"), ("SYK", "Stryker Corporation"),
    ("BSX", "Boston Scientific"), ("ISRG", "Intuitive Surgical"),
    ("VRTX", "Vertex Pharmaceuticals"), ("REGN", "Regeneron Pharmaceuticals"),
    ("MRNA", "Moderna Inc."), ("BIIB", "Biogen Inc."),
    ("ZTS", "Zoetis Inc."), ("EW", "Edwards Lifesciences"),
    ("BDX", "Becton Dickinson"), ("HCA", "HCA Healthcare"),
    ("MCK", "McKesson Corporation"), ("COR", "Cencora Inc."),
    # ── Consumer discretionary / retail ──────────────────────────
    ("HD", "Home Depot Inc."), ("MCD", "McDonald's Corporation"),
    ("NKE", "Nike Inc."), ("SBUX", "Starbucks Corporation"),
    ("LOW", "Lowe's Companies"), ("TJX", "TJX Companies"),
    ("BKNG", "Booking Holdings"), ("ABNB", "Airbnb Inc."),
    ("MAR", "Marriott International"), ("HLT", "Hilton Worldwide"),
    ("CMG", "Chipotle Mexican Grill"), ("YUM", "Yum! Brands"),
    ("ROST", "Ross Stores"), ("LULU", "Lululemon Athletica"),
    ("F", "Ford Motor Company"), ("GM", "General Motors"),
    ("ORLY", "O'Reilly Automotive"), ("AZO", "AutoZone Inc."),
    ("DHI", "D.R. Horton"), ("LEN", "Lennar Corporation"),
    ("EBAY", "eBay Inc."), ("ETSY", "Etsy Inc."),
    # ── Consumer staples ─────────────────────────────────────────
    ("WMT", "Walmart Inc."), ("PG", "Procter & Gamble"),
    ("COST", "Costco Wholesale"), ("KO", "Coca-Cola Company"),
    ("PEP", "PepsiCo Inc."), ("TGT", "Target Corporation"),
    ("MDLZ", "Mondelez International"), ("CL", "Colgate-Palmolive"),
    ("KMB", "Kimberly-Clark Corporation"), ("GIS", "General Mills"),
    ("KHC", "Kraft Heinz Company"), ("STZ", "Constellation Brands"),
    ("MNST", "Monster Beverage"), ("HSY", "Hershey Company"),
    ("KR", "Kroger Co."), ("SYY", "Sysco Corporation"),
    # ── Energy ───────────────────────────────────────────────────
    ("XOM", "Exxon Mobil Corporation"), ("CVX", "Chevron Corporation"),
    ("COP", "ConocoPhillips"), ("EOG", "EOG Resources"),
    ("SLB", "Schlumberger NV"), ("PSX", "Phillips 66"),
    ("MPC", "Marathon Petroleum"), ("VLO", "Valero Energy"),
    ("OXY", "Occidental Petroleum"), ("WMB", "Williams Companies"),
    ("KMI", "Kinder Morgan"), ("PXD", "Pioneer Natural Resources"),
    # ── Industrials / aerospace / transport ──────────────────────
    ("GE", "General Electric Company"), ("CAT", "Caterpillar Inc."),
    ("BA", "Boeing Company"), ("RTX", "RTX Corporation"),
    ("HON", "Honeywell International"), ("UNP", "Union Pacific Corporation"),
    ("LMT", "Lockheed Martin"), ("DE", "Deere & Company"),
    ("UPS", "United Parcel Service"), ("FDX", "FedEx Corporation"),
    ("CSX", "CSX Corporation"), ("NSC", "Norfolk Southern"),
    ("MMM", "3M Company"), ("EMR", "Emerson Electric"),
    ("ETN", "Eaton Corporation"), ("ITW", "Illinois Tool Works"),
    ("PH", "Parker Hannifin"), ("GD", "General Dynamics"),
    ("NOC", "Northrop Grumman"), ("WM", "Waste Management"),
    ("DAL", "Delta Air Lines"), ("UAL", "United Airlines Holdings"),
    ("LUV", "Southwest Airlines"), ("JBHT", "J.B. Hunt Transport"),
    # ── Communication services / media ───────────────────────────
    ("NFLX", "Netflix Inc."), ("DIS", "Walt Disney Company"),
    ("TMUS", "T-Mobile US"), ("VZ", "Verizon Communications"),
    ("T", "AT&T Inc."), ("CMCSA", "Comcast Corporation"),
    ("CHTR", "Charter Communications"), ("WBD", "Warner Bros. Discovery"),
    ("PARA", "Paramount Global"), ("EA", "Electronic Arts"),
    ("TTWO", "Take-Two Interactive"), ("SPOT", "Spotify Technology"),
    ("ROKU", "Roku Inc."), ("PINS", "Pinterest Inc."),
    ("SNAP", "Snap Inc."), ("MTCH", "Match Group"),
    # ── Semiconductors / hardware ────────────────────────────────
    ("AMD", "Advanced Micro Devices"), ("QCOM", "Qualcomm Inc."),
    ("INTC", "Intel Corporation"), ("TXN", "Texas Instruments"),
    ("MU", "Micron Technology"), ("AMAT", "Applied Materials"),
    ("LRCX", "Lam Research"), ("ADI", "Analog Devices"),
    ("KLAC", "KLA Corporation"), ("MRVL", "Marvell Technology"),
    ("MCHP", "Microchip Technology"), ("ON", "ON Semiconductor"),
    ("NXPI", "NXP Semiconductors"), ("SWKS", "Skyworks Solutions"),
    ("HPQ", "HP Inc."), ("DELL", "Dell Technologies"),
    ("NTAP", "NetApp Inc."), ("WDC", "Western Digital"),
    # ── Software / cloud / SaaS ──────────────────────────────────
    ("CRM", "Salesforce Inc."), ("ADBE", "Adobe Inc."),
    ("NOW", "ServiceNow Inc."), ("INTU", "Intuit Inc."),
    ("CSCO", "Cisco Systems"), ("IBM", "International Business Machines"),
    ("PLTR", "Palantir Technologies"), ("SNOW", "Snowflake Inc."),
    ("DDOG", "Datadog Inc."), ("CRWD", "CrowdStrike Holdings"),
    ("ZS", "Zscaler Inc."), ("NET", "Cloudflare Inc."),
    ("PANW", "Palo Alto Networks"), ("FTNT", "Fortinet Inc."),
    ("SHOP", "Shopify Inc."), ("WDAY", "Workday Inc."),
    ("TEAM", "Atlassian Corporation"), ("ZM", "Zoom Communications"),
    ("DOCU", "DocuSign Inc."), ("OKTA", "Okta Inc."),
    ("MDB", "MongoDB Inc."), ("HUBS", "HubSpot Inc."),
    ("U", "Unity Software"), ("RBLX", "Roblox Corporation"),
    # ── Utilities ────────────────────────────────────────────────
    ("NEE", "NextEra Energy"), ("DUK", "Duke Energy"),
    ("SO", "Southern Company"), ("D", "Dominion Energy"),
    ("AEP", "American Electric Power"), ("EXC", "Exelon Corporation"),
    ("XEL", "Xcel Energy"), ("ED", "Consolidated Edison"),
    ("PCG", "PG&E Corporation"), ("SRE", "Sempra"),
    # ── REITs / real estate ──────────────────────────────────────
    ("PLD", "Prologis Inc."), ("AMT", "American Tower"),
    ("EQIX", "Equinix Inc."), ("CCI", "Crown Castle"),
    ("PSA", "Public Storage"), ("O", "Realty Income Corporation"),
    ("WELL", "Welltower Inc."), ("DLR", "Digital Realty Trust"),
    ("SPG", "Simon Property Group"), ("AVB", "AvalonBay Communities"),
    # ── Materials ────────────────────────────────────────────────
    ("LIN", "Linde plc"), ("APD", "Air Products and Chemicals"),
    ("SHW", "Sherwin-Williams"), ("ECL", "Ecolab Inc."),
    ("DD", "DuPont de Nemours"), ("DOW", "Dow Inc."),
    ("FCX", "Freeport-McMoRan"), ("NEM", "Newmont Corporation"),
    ("NUE", "Nucor Corporation"), ("STLD", "Steel Dynamics"),
    # ── Other consumer / staples ──────────────────────────────────
    ("MO", "Altria Group"), ("PM", "Philip Morris International"),
    ("PYPL", "PayPal Holdings"), ("SQ", "Block Inc."),
    ("UBER", "Uber Technologies"), ("LYFT", "Lyft Inc."),
    ("DASH", "DoorDash Inc."), ("COIN", "Coinbase Global"),
    ("HOOD", "Robinhood Markets"), ("SOFI", "SoFi Technologies"),
    # ── Index / sector ETFs commonly searched ────────────────────
    ("SPY", "SPDR S&P 500 ETF"), ("QQQ", "Invesco QQQ Trust"),
    ("DIA", "SPDR Dow Jones Industrial Average ETF"),
    ("IWM", "iShares Russell 2000 ETF"),
    ("VTI", "Vanguard Total Stock Market ETF"),
    ("VOO", "Vanguard S&P 500 ETF"),
    ("XLK", "Technology Select Sector SPDR Fund"),
    ("XLF", "Financial Select Sector SPDR Fund"),
    ("XLE", "Energy Select Sector SPDR Fund"),
    ("XLV", "Health Care Select Sector SPDR Fund"),
    ("XLY", "Consumer Discretionary Select Sector SPDR Fund"),
    ("XLP", "Consumer Staples Select Sector SPDR Fund"),
    ("XLI", "Industrial Select Sector SPDR Fund"),
    ("XLU", "Utilities Select Sector SPDR Fund"),
    ("XLB", "Materials Select Sector SPDR Fund"),
    ("XLRE", "Real Estate Select Sector SPDR Fund"),
    ("XLC", "Communication Services Select Sector SPDR Fund"),
    ("ARKK", "ARK Innovation ETF"), ("SMH", "VanEck Semiconductor ETF"),
    ("GLD", "SPDR Gold Trust"), ("SLV", "iShares Silver Trust"),
    ("USO", "United States Oil Fund"), ("TLT", "iShares 20+ Year Treasury Bond ETF"),
    ("HYG", "iShares iBoxx High Yield Corporate Bond ETF"),
    ("EEM", "iShares MSCI Emerging Markets ETF"),
    ("EFA", "iShares MSCI EAFE ETF"),
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
