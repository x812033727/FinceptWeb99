from pydantic import BaseModel
from typing import Any


class OHLCVBar(BaseModel):
    time: str | int         # "YYYY-MM-DD" for daily; Unix ms for intraday
    open: float
    high: float
    low: float
    close: float
    volume: int


class QuoteResponse(BaseModel):
    symbol: str
    market: str
    name: str
    price: float
    change: float
    change_pct: float
    volume: int
    open: float | None
    high: float | None
    low: float | None
    prev_close: float | None
    market_cap: float | None
    currency: str
    ts: int                 # Unix ms UTC
    is_market_open: bool
    # Which upstream actually served this quote — lets the frontend mark
    # rows that fell back to a slower / less complete provider, and
    # surfaces "all sources blocked" as a distinct UI state.
    data_source: str = "unavailable"


class FundamentalsResponse(BaseModel):
    symbol: str
    market: str
    name: str | None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    eps: float | None = None
    dividend_yield: float | None = None
    beta: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    description: str | None = None
    fetched_at: str
    # Same convention as QuoteResponse / ScreenerItem.data_source:
    # "polygon" | "yfinance" | "unavailable" tells the UI which upstream
    # served the row, or "unavailable" when both providers were blocked
    # (rare — get_fundamentals only caches non-empty payloads).
    data_source: str = "unavailable"


class FinancialsResponse(BaseModel):
    symbol: str
    source: str
    data: Any               # varies by source (polygon vs yfinance)


class ScreenerItem(BaseModel):
    symbol: str
    market: str
    name: str
    price: float
    change_pct: float
    volume: int
    market_cap: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    dividend_yield: float | None = None       # in percent (3.5 = 3.5%)
    sector: str | None = None
    # "polygon" | "yfinance" | "stooq" | "unavailable" — same convention
    # as QuoteResponse.data_source. "unavailable" rows are universe
    # placeholders (price=0) included so the page renders a clickable
    # list when every upstream is blocked; the UI uses this to mark them.
    data_source: str = "unavailable"


class MacroDataPoint(BaseModel):
    date: str
    value: float | None
