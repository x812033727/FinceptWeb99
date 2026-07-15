from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field

from api.market_data_quality import DataQualityMeta


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
    meta: DataQualityMeta | None = None


class TWOHLCVBar(BaseModel):
    time: str               # "YYYY-MM-DD"
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int
    data_source: str = "unknown"
    meta: DataQualityMeta | None = None


class TWFundamentalsResponse(BaseModel):
    symbol: str
    market: str
    exchange: str | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    dividend_yield: float | None = None
    fetched_at: str | None = None
    data_source: str = "unavailable"
    meta: DataQualityMeta | None = None


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


class FactorValue(BaseModel):
    raw: float | None = None
    z: float | None = None


class FactorCandidate(BaseModel):
    rank: int
    symbol: str
    name_zh: str | None = None
    industry: str | None = None
    price: float | None = None
    price_session: str | None = None
    fundamentals_as_of: str | None = None
    quality_period_end: str | None = None
    quality_available_on: str | None = None
    score: float
    composite_z: float
    raw_composite_z: float
    sector_adjustment: float | None = None
    factor_coverage: float
    missing_factors: list[str]
    factors: dict[str, FactorValue]


class FactorQuality(BaseModel):
    status: str
    flags: list[str]
    sources: list[str]
    universe_size: int | None = None
    eligible_count: int | None = None
    returned_count: int | None = None
    momentum_coverage_pct: float | None = None
    quality_factor_coverage_pct: float | None = None
    adjusted_price_coverage_pct: float | None = None
    point_in_time_universe: bool | None = None
    classification_coverage_pct: float | None = None
    security_master_coverage_pct: float | None = None
    sector_coverage_pct: float | None = None
    sector_neutral_applied: bool | None = None
    price_limit_history_available: bool | None = None
    suspension_history_available: bool | None = None
    benchmark_requested: str | None = None
    benchmark_used: str | None = None
    benchmark_history_available: bool | None = None
    benchmark_coverage_pct: float | None = None
    factor_forward_return_coverage_pct: float | None = None
    stale_fundamentals_excluded: int | None = None
    stale_price_history_excluded: int | None = None
    future_dated_inputs_excluded: int | None = None


class FactorRankingResponse(BaseModel):
    market: str
    as_of: str
    profile: str
    methodology_version: str
    weights: dict[str, float]
    candidates: list[FactorCandidate]
    quality: FactorQuality
    methodology: dict[str, str]
    sector_neutral: bool
    weight_source: str = "profile"
    model_id: UUID | None = None


class FactorValidationPeriod(BaseModel):
    anchor: str
    holdings: list[str]
    holding_count: int
    turnover: float
    gross_return_pct: float
    cost_pct: float
    net_return_pct: float
    benchmark_return_pct: float
    excess_return_pct: float
    benchmark_volatility_pct: float | None = None
    market_regime: str | None = None
    forward_return_observation_count: int = 0
    forward_return_universe_count: int = 0
    forward_return_coverage_pct: float = 0
    rank_ic: dict[str, float | None] = Field(default_factory=dict)
    quintile_returns_pct: list[float | None] = Field(default_factory=list)
    top_bottom_spread_pct: float | None = None
    factor_weights: dict[str, float] = Field(default_factory=dict)
    weight_source_period_count: int = 0
    weight_fallback_reason: str | None = None
    quality_status: str
    quality_flags: list[str] = Field(default_factory=list)
    classification_coverage_pct: float = 0
    sector_coverage_pct: float = 0
    sector_neutral_applied: bool = False
    average_fill_pct: float = 0
    impact_cost_pct: float = 0
    capacity_limited_count: int = 0
    deferred_trade_count: int = 0
    blocked_entry_count: int = 0
    blocked_exit_count: int = 0
    capacity_blocked_count: int = 0


class FactorSignalDiagnostic(BaseModel):
    period_count: int
    average_rank_ic: float | None = None
    median_rank_ic: float | None = None
    positive_ic_rate_pct: float | None = None
    ic_t_stat: float | None = None
    p_value: float | None = None
    annualized_ic_ir: float | None = None
    holm_adjusted_p_value: float | None = None
    significant_after_holm_5pct: bool = False


class FactorQuantileAnalysis(BaseModel):
    period_count: int
    average_returns_pct: list[float | None]
    average_top_bottom_spread_pct: float | None = None
    positive_spread_rate_pct: float | None = None


class FactorDecayDiagnostic(BaseModel):
    average_rank_ic_by_horizon: dict[str, float | None]
    peak_absolute_ic_horizon: int | None = None
    direction_consistent: bool | None = None


class FactorWeightRange(BaseModel):
    minimum: float | None = None
    maximum: float | None = None
    latest: float | None = None


class FactorWeightStability(BaseModel):
    mode: str
    base_weights: dict[str, float]
    adaptive_period_count: int
    fallback_period_count: int
    average_weight_turnover_pct: float
    maximum_weight_turnover_pct: float
    factor_ranges: dict[str, FactorWeightRange]


class FactorValidationResponse(BaseModel):
    market: str
    profile: str
    methodology_version: str
    start_date: str
    end_date: str
    top_n: int
    holding_sessions: int
    transaction_cost_bps: float
    portfolio_notional_twd: float
    max_participation_rate: float
    impact_coefficient_bps: float
    benchmark_requested: str
    benchmark_used: str
    weight_mode: str
    periods: list[FactorValidationPeriod]
    summary: dict[str, float | int | None]
    regime_analysis: dict[str, dict[str, float | int | None]]
    factor_diagnostics: dict[str, FactorSignalDiagnostic]
    factor_correlation_matrix: dict[str, dict[str, float | None]]
    quantile_analysis: FactorQuantileAnalysis
    sensitivity_analysis: dict[str, dict[str, dict[str, float | int | None]]]
    factor_decay_analysis: dict[str, FactorDecayDiagnostic]
    weight_stability: FactorWeightStability
    quality: FactorQuality
    methodology: dict[str, str]
    sector_neutral: bool


class FactorResearchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    start_date: date
    end_date: date
    profile: str = Field(default="balanced", pattern="^(balanced|value|momentum|defensive|income)$")
    top_n: int = Field(default=20, ge=5, le=100)
    holding_sessions: int = Field(default=21, ge=5, le=63)
    transaction_cost_bps: float = Field(default=20, ge=0, le=200)
    sector_neutral: bool = True
    portfolio_notional_twd: float = Field(default=10_000_000, ge=100_000, le=1_000_000_000)
    max_participation_rate: float = Field(default=.05, gt=0, le=.2)
    impact_coefficient_bps: float = Field(default=10, ge=0, le=100)
    benchmark: str = Field(default="taiex_total_return", pattern="^(taiex_total_return|equal_weight)$")
    weight_mode: str = Field(default="walk_forward", pattern="^(fixed|walk_forward)$")
    auto_promote: bool = False


class FactorPromotionCheck(BaseModel):
    name: str
    actual: Any = None
    operator: str
    threshold: Any = None
    passed: bool


class FactorPromotionGate(BaseModel):
    eligible: bool
    checks: list[FactorPromotionCheck]
    failed_checks: list[str]
    threshold_version: str


class FactorResearchRunSummary(BaseModel):
    id: UUID
    name: str | None = None
    profile: str
    methodology_version: str
    parameters: dict[str, Any]
    summary: dict[str, Any]
    gate_result: FactorPromotionGate
    created_at: datetime


class FactorResearchRunDetail(FactorResearchRunSummary):
    result: FactorValidationResponse


class FactorResearchRunList(BaseModel):
    items: list[FactorResearchRunSummary]
    total: int
    limit: int
    offset: int


class FactorModelVersionResponse(BaseModel):
    id: UUID
    profile: str
    version_number: int
    methodology_version: str
    status: str
    weights: dict[str, float]
    metrics: dict[str, Any]
    gate_result: FactorPromotionGate
    source_run_id: UUID
    promoted_at: datetime | None = None
    promotion_note: str | None = None
    created_at: datetime


class FactorResearchCreated(BaseModel):
    run: FactorResearchRunSummary
    model: FactorModelVersionResponse


class FactorPortfolioRequest(BaseModel):
    as_of: date | None = None
    profile: str = Field(default="balanced", pattern="^(balanced|value|momentum|defensive|income)$")
    sector_neutral: bool = True
    weight_source: str = Field(default="champion", pattern="^(champion|profile)$")
    candidate_count: int = Field(default=30, ge=10, le=100)
    portfolio_notional_twd: float = Field(default=10_000_000, ge=100_000, le=1_000_000_000)
    max_position_weight: float = Field(default=.10, ge=.02, le=.50)
    max_sector_weight: float = Field(default=.30, ge=.10, le=1)
    target_volatility: float = Field(default=.20, ge=.05, le=1)
    max_tracking_error: float = Field(default=.12, ge=.02, le=1)
    turnover_budget: float = Field(default=.50, ge=0, le=1)
    minimum_invested_weight: float = Field(default=.80, ge=.20, le=1)
    max_participation_rate: float = Field(default=.05, gt=0, le=.20)
    risk_aversion: float = Field(default=2, ge=0, le=20)
    current_weights: dict[str, float] | None = None


class FactorPortfolioPosition(BaseModel):
    symbol: str
    name_zh: str | None = None
    industry: str | None = None
    price: float | None = None
    weight: float
    notional_twd: float
    factor_score: float
    liquidity_cap: float
    average_daily_value_twd: float
    risk_contribution: float


class FactorPortfolioConstraint(BaseModel):
    name: str
    actual: float
    limit: float
    operator: str
    passed: bool
    binding: bool


class FactorPortfolioQuality(BaseModel):
    status: str
    flags: list[str]
    requested_candidate_count: int
    eligible_candidate_count: int
    return_observations: int
    excluded: list[dict[str, str]]
    benchmark: str
    adjusted_price_history_used: bool
    adjusted_price_coverage_pct: float


class FactorPortfolioResponse(BaseModel):
    market: str
    as_of: str
    profile: str
    methodology_version: str
    factor_methodology_version: str
    weight_source: str
    model_id: UUID | None = None
    converged: bool
    solver_message: str
    positions: list[FactorPortfolioPosition]
    summary: dict[str, float]
    risk_comparison: dict[str, float | None] = Field(default_factory=dict)
    sector_weights: dict[str, float]
    constraints: list[FactorPortfolioConstraint]
    quality: FactorPortfolioQuality
    methodology: dict[str, str]


class FactorRebalancePreviewRequest(BaseModel):
    portfolio_id: UUID
    as_of: date | None = None
    profile: str = Field(default="balanced", pattern="^(balanced|value|momentum|defensive|income)$")
    sector_neutral: bool = True
    weight_source: str = Field(default="champion", pattern="^(champion|profile)$")
    candidate_count: int = Field(default=30, ge=10, le=100)
    additional_cash_twd: float = Field(default=0, ge=0, le=1_000_000_000)
    max_position_weight: float = Field(default=.10, ge=.02, le=.50)
    max_sector_weight: float = Field(default=.30, ge=.10, le=1)
    target_volatility: float = Field(default=.20, ge=.05, le=1)
    max_tracking_error: float = Field(default=.12, ge=.02, le=1)
    turnover_budget: float = Field(default=.50, ge=0, le=1)
    minimum_invested_weight: float = Field(default=.80, ge=.20, le=1)
    max_participation_rate: float = Field(default=.05, gt=0, le=.20)
    risk_aversion: float = Field(default=2, ge=0, le=20)
    allow_odd_lot: bool = True
    min_trade_pct: float = Field(default=.10, ge=0, le=10)
    fee_bps: float = Field(default=14.25, ge=0, le=100)
    minimum_fee_twd: float = Field(default=20, ge=0, le=10_000)
    stock_sell_tax_bps: float = Field(default=30, ge=0, le=100)
    etf_sell_tax_bps: float = Field(default=10, ge=0, le=100)
    slippage_bps: float = Field(default=5, ge=0, le=500)
    impact_coefficient_bps: float = Field(default=10, ge=0, le=500)
    max_impact_bps: float = Field(default=100, ge=0, le=1_000)
    sell_tax_bps_by_symbol: dict[
        str, Annotated[float, Field(ge=0, le=100)]
    ] | None = None


class TWSecurityMasterResponse(BaseModel):
    symbol: str
    effective_from: date
    effective_to: date | None = None
    name_zh: str | None = None
    exchange: str
    instrument_type: str
    asset_class: str
    is_etf: bool
    is_bond_etf: bool
    is_leveraged: bool
    is_inverse: bool
    board_lot_size: int
    odd_lot_size: int
    sell_tax_bps: float
    tax_rule_code: str
    source: str
    classification_source_url: str | None = None
    tax_source_url: str | None = None
    confidence: str
    is_manual_override: bool
    override_reason: str | None = None
    overridden_by: str | None = None
    captured_at: datetime | None = None
    fallback: bool = False


class TWSecurityMasterOverrideRequest(BaseModel):
    effective_from: date
    effective_to: date | None = None
    name_zh: str | None = None
    exchange: str | None = Field(default=None, max_length=10)
    instrument_type: str | None = Field(default=None, max_length=32)
    asset_class: str | None = Field(default=None, max_length=24)
    is_etf: bool | None = None
    is_bond_etf: bool | None = None
    is_leveraged: bool | None = None
    is_inverse: bool | None = None
    board_lot_size: int | None = Field(default=None, ge=1, le=1_000_000)
    odd_lot_size: int | None = Field(default=None, ge=1, le=1_000_000)
    sell_tax_bps: float | None = Field(default=None, ge=0, le=100)
    tax_rule_code: str | None = Field(default=None, max_length=48)
    classification_source_url: str | None = None
    tax_source_url: str | None = None
    confidence: str | None = Field(default=None, max_length=24)
    reason: str = Field(min_length=3, max_length=1000)


class FactorRebalanceTrade(BaseModel):
    symbol: str
    side: str
    quantity: int
    mid_price_twd: float
    execution_price_twd: float
    gross_value_twd: float
    fee_twd: float
    tax_twd: float
    implementation_shortfall_twd: float
    total_cost_twd: float
    impact_bps: float
    participation_rate: float
    liquidity_data_available: bool
    current_quantity: float
    target_weight: float
    board_lot_size: int = 1000
    sell_tax_bps: float
    trading_rule_source: str
    tax_rule_code: str | None = None


class FactorRebalancePreviewResponse(BaseModel):
    portfolio_id: str
    currency: str
    portfolio_name: str
    portfolio_base_currency: str
    portfolio_notional_twd: float
    ledger_cash_twd: float = 0
    additional_cash_twd: float = 0
    target_portfolio: FactorPortfolioResponse
    trades: list[FactorRebalanceTrade]
    post_positions: list[dict[str, Any]]
    cost_scenarios: list[dict[str, Any]]
    frozen: list[dict[str, Any]]
    excluded: list[dict[str, str]]
    summary: dict[str, Any]
    quality_flags: list[str]
    methodology: dict[str, str]
    preview_only: bool
