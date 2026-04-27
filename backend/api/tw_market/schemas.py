from pydantic import BaseModel


class TWQuoteResponse(BaseModel):
    symbol: str
    market: str
    exchange: str           # "TWSE" | "TPEx"
    name_zh: str
    price: float
    change: float | None
    change_pct: float | None
    volume: int
    open: float | None
    high: float | None
    low: float | None
    currency: str           # "TWD"
    ts: int                 # Unix ms UTC
    tz: str                 # "Asia/Taipei"
    is_market_open: bool
    is_etf: bool = False
    # "twse" (realtime) | "finmind" (latest close fallback) | "unavailable".
    # Same convention as the US QuoteResponse.data_source.
    data_source: str = "unavailable"


class TWOHLCVBar(BaseModel):
    time: str               # "YYYY-MM-DD"
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int


class InstitutionalRow(BaseModel):
    date: str
    symbol: str
    fini_buy: int = 0
    fini_sell: int = 0
    sitc_buy: int = 0
    sitc_sell: int = 0
    dealer_buy: int = 0
    dealer_sell: int = 0

    @property
    def fini_net(self) -> int:
        return self.fini_buy - self.fini_sell

    @property
    def sitc_net(self) -> int:
        return self.sitc_buy - self.sitc_sell

    @property
    def dealer_net(self) -> int:
        return self.dealer_buy - self.dealer_sell


class MarginRow(BaseModel):
    date: str
    symbol: str
    margin_purchase: int = 0
    margin_balance: int = 0
    short_sale: int = 0
    short_balance: int = 0


class RevenueRow(BaseModel):
    date: str
    symbol: str
    revenue: int            # thousands NTD
    revenue_mom: float | None = None   # 月增率 %
    revenue_yoy: float | None = None   # 年增率 %


class TWScreenerItem(BaseModel):
    symbol: str
    market: str
    exchange: str
    name_zh: str
    price: float | None
    change: float | None = None
    change_pct: float | None = None
    volume: int
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    dividend_yield: float | None = None     # %
    # Source the row came from. TW screener is single-upstream (TWSE
    # OpenAPI) so this is "twse" or, on a transient TWSE outage, the
    # endpoint returns an empty list rather than tagged-unavailable rows.
    data_source: str = "twse"


class TWIndexResponse(BaseModel):
    index: str
    value: float | None
    change: float | None
    time: str | None
