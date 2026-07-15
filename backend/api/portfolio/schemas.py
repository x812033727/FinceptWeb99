from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class PortfolioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    # No default — caller must pick (3-letter ISO 4217 / common stable
    # ticker). Backend then converts every holding's market value to
    # this base currency on read. Mixed-market portfolios (TW + US +
    # crypto) need an explicit choice; defaulting to USD silently
    # converted TWD positions even when the user expected TWD totals.
    currency: str = Field(..., min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, v: str) -> str:
        return v.upper()


class TransactionCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: str = Field(..., pattern=r"^(US|TW|CRYPTO|us|tw|crypto)$")
    tx_type: str = Field(..., pattern=r"^(buy|sell|dividend|BUY|SELL|DIVIDEND)$")
    quantity: float = Field(..., gt=0)
    price: float = Field(..., ge=0)
    # Allow None / 0 so the service auto-stamps the trade-day FX rate.
    fx_rate: float | None = Field(default=None, ge=0)
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


class TransactionImportRequest(BaseModel):
    """Broker-neutral transaction rows parsed from a CSV by the client."""

    rows: list[dict[str, Any]] = Field(..., min_length=1, max_length=500)
    dry_run: bool = True


class TransactionImportError(BaseModel):
    row: int
    field: str | None = None
    message: str


class TransactionImportResponse(BaseModel):
    valid: bool
    valid_count: int
    imported_count: int
    duplicate: bool = False
    import_id: str | None = None
    imported_at: datetime | None = None
    errors: list[TransactionImportError] = Field(default_factory=list)


class TransactionImportInstrumentResponse(BaseModel):
    symbol: str
    market: str


class TransactionImportBatchResponse(BaseModel):
    id: UUID
    row_count: int
    linked_count: int
    provenance_complete: bool
    first_tx_date: date | None
    last_tx_date: date | None
    instruments: list[TransactionImportInstrumentResponse]
    imported_at: datetime


class TransactionImportRollbackResponse(BaseModel):
    import_id: UUID
    removed_count: int


class PortfolioUpdate(BaseModel):
    """All fields optional — PATCH semantics. Empty body is a no-op."""
    name: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class TransactionUpdate(BaseModel):
    """All fields optional — PATCH semantics."""
    symbol: str | None = Field(default=None, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: str | None = Field(default=None, pattern=r"^(US|TW|CRYPTO|us|tw|crypto)$")
    tx_type: str | None = Field(default=None, pattern=r"^(buy|sell|dividend|BUY|SELL|DIVIDEND)$")
    quantity: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, ge=0)
    fx_rate: float | None = Field(default=None, gt=0)
    tx_date: date | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("market")
    @classmethod
    def upper_market(cls, v: str | None) -> str | None:
        return v.upper() if v else v

    @field_validator("tx_type")
    @classmethod
    def lower_tx_type(cls, v: str | None) -> str | None:
        return v.lower() if v else v


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
    net_weight_pct: float | None = None


class PortfolioSummary(BaseModel):
    id: str
    name: str
    currency: str
    total_value: float
    total_cost: float
    total_pnl: float
    total_pnl_pct: float
    cash_balances: dict[str, float] = Field(default_factory=dict)
    cash_value: float = 0
    net_liquidation_value: float | None = None
    holdings: list[HoldingResponse]


class PortfolioListItem(BaseModel):
    id: UUID
    name: str
    currency: str


class PerformancePoint(BaseModel):
    date: str
    value: float


class AttributionPosition(BaseModel):
    symbol: str
    market: str
    start_date: date
    end_date: date
    start_quantity: float
    end_quantity: float
    start_value: float
    end_value: float
    net_cash_flow: float
    weighted_cash_flow: float
    pnl_after_flows: float
    denominator: float
    position_return_pct: float | None
    start_weight_pct: float | None
    contribution_pct: float | None


class AttributionExcluded(BaseModel):
    symbol: str
    market: str
    reason: str


class AttributionMarket(BaseModel):
    market: str
    start_weight_pct: float | None
    market_return_pct: float | None
    contribution_pct: float | None
    pnl_after_flows: float


class PortfolioAttributionResponse(BaseModel):
    portfolio_id: str
    currency: str
    methodology_version: str
    requested_days: int
    period_start: date
    period_end: date
    empty: bool
    portfolio_return_pct: float | None
    benchmark: str | None
    benchmark_return_pct: float | None
    active_return_pct: float | None
    denominator: float
    markets: list[AttributionMarket]
    positions: list[AttributionPosition]
    excluded: list[AttributionExcluded]
    disclaimer: str


class OptimiseRequest(BaseModel):
    target_risk: str = "medium"   # "low" | "medium" | "high"
    max_weight: float = 1.0


class OptimiseResponse(BaseModel):
    weights: dict[str, float]
    metrics: dict
    converged: bool = True
    frontier: list[dict] = []


# ── Rebalance plan (feature C5) ───────────────────────────────────

class RebalancePlanRequest(BaseModel):
    target: str = "optimise"          # "optimise" | "equal_weight" | "custom"
    target_risk: str = "medium"       # optimise 模式的風險偏好
    max_weight: float = 1.0
    custom_weights: dict[str, float] | None = None
    fee_bps: float = 0.0              # 券商費率(雙向皆計)
    min_trade_pct: float = 1.0        # 小於總值此 % 的調整視為塵埃、略過
    allow_odd_lot: bool = False       # TW 允許零股(否則 1000 股整張)

    @field_validator("target")
    @classmethod
    def _target_ok(cls, v: str) -> str:
        if v not in ("optimise", "equal_weight", "custom"):
            raise ValueError("target must be optimise | equal_weight | custom")
        return v


class RebalanceTrade(BaseModel):
    symbol: str
    market: str
    side: str
    quantity: float
    est_price: float
    price_currency: str
    est_value: float
    est_fee: float
    current_weight_pct: float
    target_weight_pct: float


class RebalanceFrozen(BaseModel):
    symbol: str
    market: str
    current_value: float
    reason: str


class RebalancePlanResponse(BaseModel):
    portfolio_id: str
    currency: str
    total_value: float
    target: str = "optimise"
    trades: list[RebalanceTrade] = []
    frozen: list[RebalanceFrozen] = []
    summary: dict


# ── Risk dashboard (feature C1) ───────────────────────────────────

class RiskVaREntry(BaseModel):
    """One VaR figure — (method × confidence level). Shape mirrors the
    dicts returned by analytics/risk.py so nothing is re-mapped."""
    method: str                      # historical | parametric | monte_carlo
    confidence_level: float
    horizon_days: int = 1
    var_pct: float
    var_amount: float
    cvar_pct: float | None = None
    n_simulations: int | None = None
    annualised_return: float | None = None
    annualised_vol: float | None = None


class RiskMetrics(BaseModel):
    """Output of analytics.risk.portfolio_metrics, verbatim."""
    annualised_return: float
    annualised_volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown: float
    beta: float | None = None
    alpha: float | None = None


class RiskWeightRow(BaseModel):
    symbol: str
    market: str
    weight_pct: float
    # % of total portfolio variance this holding contributes
    # (marginal-contribution decomposition). None when the holding was
    # excluded from the return matrix (insufficient history).
    risk_contribution_pct: float | None = None


class RiskCorrelationMatrix(BaseModel):
    symbols: list[str]
    matrix: list[list[float]]        # symbols × symbols, in `symbols` order


class RiskConcentrationWarning(BaseModel):
    kind: str                        # single_position | market_bucket
    key: str                         # symbol or market name
    weight_pct: float
    threshold_pct: float


class RiskExcludedHolding(BaseModel):
    symbol: str
    market: str
    reason: str                      # insufficient_history
    observations: int = 0


class PortfolioRiskResponse(BaseModel):
    portfolio_id: str
    currency: str
    as_of: str
    portfolio_value: float
    observations: int                # aligned daily-return count used
    empty: bool = False              # True → portfolio has no holdings
    benchmark: str | None = None     # SPY | _TAIEX_TR | None (fetch failed)
    metrics: RiskMetrics | None = None
    var: list[RiskVaREntry] = []
    weights: list[RiskWeightRow] = []
    correlation: RiskCorrelationMatrix | None = None
    warnings: list[RiskConcentrationWarning] = []
    excluded: list[RiskExcludedHolding] = []


# ── Deterministic scenario stress test ───────────────────────────

class StressTestRequest(BaseModel):
    scenarios: list[str] | None = None
    gap_symbol: str | None = Field(default=None, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    gap_pct: float = Field(default=-20.0, ge=-100.0, le=100.0)

    @field_validator("scenarios")
    @classmethod
    def unique_scenarios(cls, value: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(value)) if value is not None else None


class StressHoldingImpact(BaseModel):
    symbol: str
    market: str
    current_value: float
    shock_pct: float
    pnl: float
    risk_contribution_pct: float
    drivers: list[str]


class StressRebalanceSuggestion(BaseModel):
    symbol: str
    action: str
    current_stressed_weight_pct: float
    target_weight_pct: float
    indicative_amount: float
    reason: str


class StressScenarioResult(BaseModel):
    scenario: str
    label: str
    pnl: float
    pnl_pct: float
    post_scenario_value: float
    holdings: list[StressHoldingImpact]
    rebalance_suggestions: list[StressRebalanceSuggestion]


class StressTestResponse(BaseModel):
    portfolio_id: str
    currency: str
    as_of: datetime
    valuation_source: str
    portfolio_value: float
    gap_symbol: str | None
    scenarios: list[StressScenarioResult]
    disclaimer: str


class TransactionResponse(BaseModel):
    id: UUID
    import_id: UUID | None = None
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


class TransactionPageResponse(BaseModel):
    items: list[TransactionResponse]
    next_cursor: str | None = None


class CashEntryCreate(BaseModel):
    currency: str = Field(..., min_length=3, max_length=3)
    amount: float = Field(..., gt=0, le=1_000_000_000_000)
    entry_type: Literal[
        "deposit", "withdrawal", "fee", "tax", "interest", "dividend",
        "refund", "adjustment_credit", "adjustment_debit",
    ]
    occurred_on: date = Field(default_factory=date.today)
    notes: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class CashEntryReverse(BaseModel):
    notes: str | None = Field(default=None, max_length=500)


class CashEntryResponse(BaseModel):
    id: UUID
    portfolio_id: UUID
    currency: str
    amount: float
    entry_type: str
    source: str
    occurred_on: date
    transaction_id: UUID | None = None
    reversal_of: UUID | None = None
    idempotency_key: str | None = None
    notes: str | None = None
    entry_metadata: dict | None = None
    is_reversed: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class CashBalanceResponse(BaseModel):
    portfolio_id: str
    base_currency: str
    balances: dict[str, float]
    total_cash_base: float
    negative_currencies: list[str]
    as_of: date | None = None


class PortfolioSnapshotResponse(BaseModel):
    id: UUID
    snapshot_date: date
    portfolio_id: UUID
    total_value_usd: float
    base_currency: str | None = None
    holdings_value_base: float | None = None
    cash_value_base: float | None = None
    total_value_base: float | None = None
    positions: list | None = None
    cash_balances: dict | None = None
    valuation_quality: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
