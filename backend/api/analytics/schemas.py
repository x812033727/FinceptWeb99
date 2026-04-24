from pydantic import BaseModel, field_validator
from typing import Any


class DCFRequest(BaseModel):
    symbol: str
    market: str = "US"
    overrides: dict[str, Any] = {}


class DCFResponse(BaseModel):
    symbol: str
    market: str
    intrinsic_value: float
    equity_value: float
    pv_fcf: float
    pv_terminal: float
    margin_of_safety: float | None
    sensitivity: dict
    scenarios: dict[str, float]


class VaRRequest(BaseModel):
    symbols: list[str]
    markets: list[str]
    weights: list[float]
    portfolio_value: float
    method: str = "all"          # "historical" | "parametric" | "monte_carlo" | "all"
    confidence: float = 0.95
    horizon_days: int = 1

    @field_validator("weights")
    @classmethod
    def weights_positive(cls, v: list[float]) -> list[float]:
        if any(w < 0 for w in v):
            raise ValueError("Weights must be non-negative")
        return v


class VaRResponse(BaseModel):
    symbols: list[str]
    weights: list[float]
    historical: dict | None = None
    parametric: dict | None = None
    monte_carlo: dict | None = None
    portfolio_metrics: dict | None = None


class BacktestRequest(BaseModel):
    symbols: list[str]
    markets: list[str]
    strategy: str = "sma_crossover"   # "sma_crossover" | "rsi_mean_reversion"
    params: dict[str, Any] = {}
    start_date: str                   # "YYYY-MM-DD"
    end_date: str
    initial_capital: float = 100_000.0


class BacktestResponse(BaseModel):
    status: str
    equity_curve: list[dict] | None = None
    trades: list[dict] | None = None
    metrics: dict | None = None
    error: str | None = None
