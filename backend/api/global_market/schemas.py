from datetime import date

from pydantic import BaseModel


class ComparePoint(BaseModel):
    date: date
    value: float


class CompareSeries(BaseModel):
    instrument: str
    market: str
    symbol: str
    base_date: date
    end_date: date
    observations: int
    return_pct: float
    max_drawdown_pct: float
    annualised_volatility_pct: float | None
    data_source: str | None
    points: list[ComparePoint]


class CompareExcluded(BaseModel):
    market: str
    symbol: str
    reason: str


class CompareHistoryResponse(BaseModel):
    period: str
    requested: list[str]
    common_base_date: date | None
    normalization: str
    currency_note: str
    series: list[CompareSeries]
    excluded: list[CompareExcluded]
