from pydantic import BaseModel, Field, field_validator
from datetime import date, datetime
from uuid import UUID


class PortfolioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, v: str) -> str:
        return v.upper()


class TransactionCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: str = Field(..., pattern=r"^(US|TW|us|tw)$")
    tx_type: str = Field(..., pattern=r"^(buy|sell|dividend|BUY|SELL|DIVIDEND)$")
    quantity: float = Field(..., gt=0)
    price: float = Field(..., ge=0)
    fx_rate: float = Field(default=1.0, gt=0)
    tx_date: date
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("market")
    @classmethod
    def upper_market(cls, v: str) -> str:
        return v.upper()

    @field_validator("tx_type")
    @classmethod
    def lower_tx_type(cls, v: str) -> str:
        return v.lower()


class HoldingResponse(BaseModel):
    id: str
    symbol: str
    market: str
    quantity: float
    avg_cost: float
    cost_currency: str
    current_price: float
    current_value: float
    cost_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    weight_pct: float


class PortfolioSummary(BaseModel):
    id: str
    name: str
    currency: str
    total_value: float
    total_cost: float
    total_pnl: float
    total_pnl_pct: float
    holdings: list[HoldingResponse]


class PortfolioListItem(BaseModel):
    id: UUID
    name: str
    currency: str


class PerformancePoint(BaseModel):
    date: str
    value: float


class OptimiseRequest(BaseModel):
    target_risk: str = "medium"   # "low" | "medium" | "high"
    max_weight: float = 1.0


class OptimiseResponse(BaseModel):
    weights: dict[str, float]
    metrics: dict
    converged: bool = True
    frontier: list[dict] = []


class TransactionResponse(BaseModel):
    id: UUID
    symbol: str
    market: str
    tx_type: str
    quantity: float
    price: float
    fx_rate: float
    tx_date: date
    notes: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("market", "tx_type", mode="before")
    @classmethod
    def enum_to_str(cls, v: object) -> str:
        return v.value if hasattr(v, "value") else str(v)
