from pydantic import BaseModel
from typing import Any, Literal
from api.market_data_quality import DataQualityMeta


class OHLCVBar(BaseModel):
    time: str | int         # "YYYY-MM-DD" for daily; Unix ms for intraday
    open: float
    high: float
    low: float
    close: float
    volume: int
    data_source: str = "unknown"
    meta: DataQualityMeta | None = None


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
    meta: DataQualityMeta | None = None


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
    meta: DataQualityMeta | None = None


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


class OptionContract(BaseModel):
    ticker: str
    contract_type: Literal["call", "put"]
    expiration_date: str
    strike_price: float
    last_price: float | None
    bid: float | None
    ask: float | None
    volume: int | None
    open_interest: int | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    data_source: str


class OptionExpiryAnalytics(BaseModel):
    expiration_date: str
    days_to_expiry: int
    contract_count: int
    call_open_interest: int
    put_open_interest: int
    put_call_open_interest_ratio: float | None
    call_volume: int
    put_volume: int
    put_call_volume_ratio: float | None
    atm_iv: float | None
    atm_call_iv: float | None
    atm_put_iv: float | None
    atm_call_strike: float | None
    atm_put_strike: float | None
    expected_move: float | None
    expected_move_pct: float | None
    put_90_iv: float | None
    put_90_strike: float | None
    call_110_iv: float | None
    call_110_strike: float | None
    wing_skew_iv_points: float | None
    max_pain: float | None
    max_pain_distance_pct: float | None
    max_pain_total_payout: float | None


class OptionAnalysisQuality(BaseModel):
    status: Literal["good", "degraded", "unavailable"]
    flags: list[str]
    sources: list[str]
    rows_received: int
    rows_usable: int
    iv_coverage_pct: float
    open_interest_coverage_pct: float


class OptionsAnalysisResponse(BaseModel):
    symbol: str
    spot: float | None
    spot_source: str | None
    as_of: str
    contracts: list[OptionContract]
    expiries: list[OptionExpiryAnalytics]
    quality: OptionAnalysisQuality
    methodology: dict[str, str]
