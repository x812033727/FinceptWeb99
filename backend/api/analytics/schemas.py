from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any

MAX_SYMBOLS = 50
MAX_OVERRIDES = 32
MAX_PARAMS = 32
SYMBOL_PATTERN = r"^[A-Za-z0-9.\-]{1,20}$"


def _bounded_overrides(v: dict[str, Any]) -> dict[str, Any]:
    if len(v) > MAX_OVERRIDES:
        raise ValueError(f"At most {MAX_OVERRIDES} override fields are allowed")
    return v


class DCFRequest(BaseModel):
    symbol: str = Field(..., pattern=SYMBOL_PATTERN)
    market: str = Field("US", pattern="^(US|TW)$")
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def _check_overrides(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _bounded_overrides(v)


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
    symbols: list[str] = Field(..., min_length=1, max_length=MAX_SYMBOLS)
    markets: list[str] = Field(..., min_length=1, max_length=MAX_SYMBOLS)
    weights: list[float] = Field(..., min_length=1, max_length=MAX_SYMBOLS)
    portfolio_value: float = Field(..., gt=0, le=1e15)
    method: str = Field("all", pattern="^(historical|parametric|monte_carlo|all)$")
    confidence: float = Field(0.95, gt=0.0, lt=1.0)
    horizon_days: int = Field(1, ge=1, le=252)

    @field_validator("symbols")
    @classmethod
    def symbols_format(cls, v: list[str]) -> list[str]:
        import re
        pat = re.compile(SYMBOL_PATTERN)
        for s in v:
            if not pat.match(s):
                raise ValueError(f"Invalid symbol: {s!r}")
        return v

    @field_validator("weights")
    @classmethod
    def weights_positive(cls, v: list[float]) -> list[float]:
        if any(w < 0 for w in v):
            raise ValueError("Weights must be non-negative")
        return v

    @model_validator(mode="after")
    def _matched_lengths(self):
        if not (len(self.symbols) == len(self.markets) == len(self.weights)):
            raise ValueError("symbols, markets, and weights must have equal length")
        return self


class VaRResponse(BaseModel):
    symbols: list[str]
    weights: list[float]
    historical: dict | None = None
    parametric: dict | None = None
    monte_carlo: dict | None = None
    portfolio_metrics: dict | None = None


class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1, max_length=MAX_SYMBOLS)
    markets: list[str] = Field(..., min_length=1, max_length=MAX_SYMBOLS)
    strategy: str = Field("sma_crossover", pattern=r"^[a-z][a-z0-9_]{0,49}$")
    params: dict[str, Any] = Field(default_factory=dict)
    start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_capital: float = Field(100_000.0, gt=0, le=1e12)
    # ── C2 risk controls — all optional / off by default ─────────
    stop_loss_pct: float | None = Field(None, gt=0, lt=1)
    take_profit_pct: float | None = Field(None, gt=0, le=10)
    trailing_stop_pct: float | None = Field(None, gt=0, lt=1)
    position_size_pct: float | None = Field(None, gt=0, le=1)
    slippage_bps: float = Field(0.0, ge=0, le=1_000)
    commission_bps: float = Field(0.0, ge=0, le=1_000)
    allow_short: bool = False

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        from analytics.backtest import STRATEGIES
        if v not in STRATEGIES:
            raise ValueError(
                f"Unknown strategy {v!r}. Available: {sorted(STRATEGIES)}"
            )
        return v

    @field_validator("symbols")
    @classmethod
    def symbols_format(cls, v: list[str]) -> list[str]:
        import re
        pat = re.compile(SYMBOL_PATTERN)
        for s in v:
            if not pat.match(s):
                raise ValueError(f"Invalid symbol: {s!r}")
        return v

    @field_validator("params")
    @classmethod
    def _check_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > MAX_PARAMS:
            raise ValueError(f"At most {MAX_PARAMS} strategy params are allowed")
        return v

    @model_validator(mode="after")
    def _date_order(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class BacktestResponse(BaseModel):
    status: str
    equity_curve: list[dict] | None = None
    # Trade dicts gain `exit_reason` (signal | stop | take_profit |
    # trailing) plus per-fill `commission` / `slippage` when any C2
    # risk-control field was supplied; `metrics` then also carries
    # `total_commission` / `total_slippage`.
    trades: list[dict] | None = None
    metrics: dict | None = None
    error: str | None = None


class StrategyParamInfo(BaseModel):
    name: str
    type: str                    # "int" | "float"
    default: float
    min: float | None = None
    max: float | None = None
    description: str = ""


class StrategyInfo(BaseModel):
    name: str
    label: str
    description: str = ""
    params: list[StrategyParamInfo] = Field(default_factory=list)
