import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiGetMock, apiPostMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(), apiPostMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ default: { get: apiGetMock, post: apiPostMock } }));

import { FactorRankingPanel } from "./FactorRankingPanel";

const quality = {
  status: "good", flags: ["unadjusted_price_history"], adjusted_price_coverage_pct: 82.5,
  sources: ["fundamentals_snapshots", "ohlcv_daily", "tw_company_info"],
  universe_size: 100, eligible_count: 80, returned_count: 1,
  momentum_coverage_pct: 90,
};

const ranking = {
  as_of: "2026-07-15", profile: "balanced",
  methodology_version: "tw-explainable-multifactor-v8",
  weight_source: "profile", model_id: null,
  sector_neutral: true,
  weights: { value: .25, quality: .15, momentum: .2, low_volatility: .15, income: .1, liquidity: .15 },
  quality,
  methodology: { model: "deterministic" },
  candidates: [{
    rank: 1, symbol: "2330", name_zh: "台積電", industry: "半導體",
    price: 1000, price_session: "2026-07-15", fundamentals_as_of: "2026-07-14",
    quality_period_end: "2026-03-31", quality_available_on: "2026-05-16",
    score: 100, composite_z: 1.5, raw_composite_z: 1.8,
    sector_adjustment: .3, factor_coverage: 1, missing_factors: [],
    factors: {
      value: { raw: .1, z: 1.2 }, quality: { raw: .3, z: .9 }, momentum: { raw: .2, z: .8 },
      low_volatility: { raw: -.15, z: .5 }, income: { raw: 2, z: -.2 },
      liquidity: { raw: 20, z: 1.1 },
    },
  }],
};

const validation = {
  profile: "balanced", start_date: "2025-07-15", end_date: "2026-07-15",
  top_n: 20, holding_sessions: 21, transaction_cost_bps: 20,
  portfolio_notional_twd: 10_000_000, max_participation_rate: .05,
  impact_coefficient_bps: 10,
  benchmark_requested: "taiex_total_return", benchmark_used: "taiex_total_return",
  weight_mode: "walk_forward",
  periods: [{ anchor: "2026-01-02", holding_count: 20, turnover: 1,
    net_return_pct: 2.5, benchmark_return_pct: 1, excess_return_pct: 1.5,
    average_fill_pct: 92, impact_cost_pct: .08, deferred_trade_count: 1,
    capacity_limited_count: 2, benchmark_volatility_pct: 18, market_regime: "bull",
    forward_return_observation_count: 100, rank_ic: { composite: .12 },
    forward_return_universe_count: 100, forward_return_coverage_pct: 100,
    quintile_returns_pct: [-1, 0, 1, 2, 3], top_bottom_spread_pct: 4,
    factor_weights: { value: .2, quality: .22, momentum: .18, low_volatility: .15, income: .1, liquidity: .15 },
    weight_source_period_count: 12, weight_fallback_reason: null }],
  summary: { period_count: 12, cumulative_return_pct: 15,
    average_period_return_pct: 1.2, average_excess_return_pct: .4,
    positive_period_rate_pct: 60, max_drawdown_pct: -5, average_turnover_pct: 25,
    average_fill_pct: 92, average_impact_cost_pct: .08, blocked_trade_count: 3,
    positive_excess_rate_pct: 66.7, annualized_information_ratio: .85,
    excess_return_t_stat: 1.9, excess_return_ci_low_pct: -.1,
    excess_return_ci_high_pct: .9 },
  regime_analysis: {
    bull: { period_count: 8, average_excess_return_pct: .6 },
    bear: { period_count: 4, average_excess_return_pct: -.2 },
    high_volatility: { period_count: 6, average_excess_return_pct: .2 },
    low_volatility: { period_count: 6, average_excess_return_pct: .5 },
  },
  factor_diagnostics: Object.fromEntries(
    ["composite", "value", "quality", "momentum", "low_volatility", "income", "liquidity"].map((signal) => [signal, {
      period_count: 12, average_rank_ic: .12, median_rank_ic: .1,
      positive_ic_rate_pct: 66.7, ic_t_stat: 2.1, p_value: .04,
      annualized_ic_ir: .8, holm_adjusted_p_value: .048,
      significant_after_holm_5pct: signal === "composite",
    }]),
  ),
  factor_correlation_matrix: Object.fromEntries(
    ["value", "quality", "momentum", "low_volatility", "income", "liquidity"].map((left) => [left,
      Object.fromEntries(["value", "quality", "momentum", "low_volatility", "income", "liquidity"].map((right) => [right, left === right ? 1 : .2])),
    ]),
  ),
  quantile_analysis: { period_count: 12, average_returns_pct: [-1, 0, 1, 2, 3],
    average_top_bottom_spread_pct: 4, positive_spread_rate_pct: 75 },
  sensitivity_analysis: {
    holding_sessions: {
      "5": { period_count: 12, average_rank_ic: .08, average_top_bottom_spread_pct: 1 },
      "21": { period_count: 12, average_rank_ic: .12, average_top_bottom_spread_pct: 4 },
      "63": { period_count: 10, average_rank_ic: .1, average_top_bottom_spread_pct: 6 },
    },
    top_n: {
      "10": { period_count: 12, average_forward_return_pct: 2 },
      "20": { period_count: 12, average_forward_return_pct: 1.5 },
      "50": { period_count: 12, average_forward_return_pct: 1 },
    },
  },
  factor_decay_analysis: Object.fromEntries(
    ["composite", "value", "quality", "momentum", "low_volatility", "income", "liquidity"].map((signal) => [signal, {
      average_rank_ic_by_horizon: { "5": .08, "21": .12, "63": .04 },
      peak_absolute_ic_horizon: 21, direction_consistent: true,
    }]),
  ),
  weight_stability: {
    mode: "walk_forward",
    base_weights: { value: .2, quality: .2, momentum: .2, low_volatility: .15, income: .1, liquidity: .15 },
    adaptive_period_count: 6, fallback_period_count: 6,
    average_weight_turnover_pct: 2.5, maximum_weight_turnover_pct: 4.2,
    factor_ranges: Object.fromEntries(
      ["value", "quality", "momentum", "low_volatility", "income", "liquidity"].map((factor) => [factor, {
        minimum: .1, maximum: .22, latest: .18,
      }]),
    ),
  },
  quality: { status: "degraded", flags: ["survivorship_bias"], sources: ["ohlcv_daily"],
    benchmark_used: "taiex_total_return", benchmark_coverage_pct: 100,
    factor_forward_return_coverage_pct: 95 },
  methodology: { validation: "rolling" },
  sector_neutral: true,
};

const registeredModels = [{
  id: "11111111-1111-4111-8111-111111111111", profile: "balanced",
  version_number: 1, methodology_version: "tw-explainable-multifactor-v8",
  status: "candidate", weights: ranking.weights,
  metrics: { period_count: 30, average_excess_return_pct: .6, composite_rank_ic: .08 },
  gate_result: { eligible: true, failed_checks: [], threshold_version: "tw-factor-promotion-v1" },
  created_at: "2026-07-15T00:00:00Z",
}];

const factorPortfolio = {
  as_of: "2026-07-15", profile: "balanced",
  methodology_version: "tw-factor-portfolio-v1",
  factor_methodology_version: "tw-explainable-multifactor-v8",
  weight_source: "profile", model_id: null, converged: true,
  solver_message: "Optimization terminated successfully",
  positions: [{ symbol: "2330", name_zh: "台積電", industry: "半導體",
    price: 1000, weight: .1, notional_twd: 1_000_000, factor_score: 99,
    liquidity_cap: .1, average_daily_value_twd: 10_000_000_000,
    risk_contribution: .2 }],
  summary: { invested_weight: .8, cash_weight: .2, annual_volatility: .15,
    tracking_error: .08, turnover: 0, weighted_factor_score: 75 },
  risk_comparison: { pre_trade_annual_volatility: .18, post_trade_annual_volatility: .15,
    pre_trade_tracking_error: .1, post_trade_tracking_error: .08,
    current_weight_coverage: 1 },
  sector_weights: { 半導體: .3 },
  constraints: [{ name: "target_volatility", actual: .15, limit: .2,
    operator: "<=", passed: true, binding: false }],
  quality: { status: "good", flags: [], requested_candidate_count: 30,
    eligible_candidate_count: 25, return_observations: 252, excluded: [],
    adjusted_price_coverage_pct: 100 },
};

const rebalancePreview = {
  portfolio_id: "11111111-1111-4111-8111-111111111111", portfolio_name: "核心",
  portfolio_notional_twd: 2_000_000, ledger_cash_twd: 500_000,
  additional_cash_twd: 0, preview_only: true,
  target_portfolio: factorPortfolio,
  trades: [{ symbol: "2330", side: "buy", quantity: 1000,
    execution_price_twd: 1001, gross_value_twd: 1_001_000,
    fee_twd: 1426, tax_twd: 0, total_cost_twd: 2426,
    impact_bps: 5, target_weight: .5 }],
  cost_scenarios: [{ name: "low", estimated_cost_twd: 2000 },
    { name: "base", estimated_cost_twd: 2426 },
    { name: "stress", estimated_cost_twd: 3426 }],
  frozen: [{ symbol: "AAPL", market: "US", reason: "non_tw_holding_outside_factor_scope" }],
  quality_flags: ["non_tw_holdings_frozen"],
  summary: { trade_count: 1, gross_turnover_twd: 1_001_000,
    estimated_total_cost_twd: 2426, estimated_cost_bps: 12.13,
    ending_cash_twd: 997_574, funding_shortfall_twd: 0, funded: true },
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><FactorRankingPanel /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("FactorRankingPanel", () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiGetMock.mockImplementation((url: string) => Promise.resolve({
      data: url === "/portfolio"
        ? [{ id: "11111111-1111-4111-8111-111111111111", name: "核心", currency: "TWD" }]
        : url.startsWith("/tw/factor-validation")
        ? validation
        : url.startsWith("/tw/factor-models") ? registeredModels : ranking,
    }));
    apiPostMock.mockImplementation((url: string) => Promise.resolve({
      data: url === "/tw/factor-portfolio" ? factorPortfolio
        : url === "/tw/factor-portfolio/rebalance-preview" ? rebalancePreview : {},
    }));
  });

  it("renders decomposed factor scores and evidence limits", async () => {
    renderPanel();
    expect(await screen.findByText("2330")).toBeInTheDocument();
    expect(screen.getByText("Factor archive coverage is healthy")).toBeInTheDocument();
    expect(screen.getByText("Price history is not corporate-action adjusted")).toBeInTheDocument();
    expect(screen.getByText(/Point-in-time as of/)).toHaveTextContent("tw-explainable-multifactor-v8");
    expect(screen.getByText("+1.20")).toBeInTheDocument();
    expect(screen.getByTitle(/Statement period 2026-03-31/)).toBeInTheDocument();
    expect(screen.getByText("Factor model registry")).toBeInTheDocument();
    expect(screen.getByText(/Challenger/)).toBeInTheDocument();
  });

  it("changes profiles and runs cost-aware rolling validation", async () => {
    renderPanel();
    await screen.findByText("2330");
    fireEvent.click(screen.getByRole("button", { name: "Value" }));
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith(
      "/tw/factor-ranking?profile=value&limit=100&sector_neutral=true",
    ));

    fireEvent.click(screen.getByRole("checkbox", { name: "Industry neutral" }));
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith(
      "/tw/factor-ranking?profile=value&limit=100&sector_neutral=false",
    ));

    fireEvent.click(screen.getByRole("button", { name: "Run validation" }));
    await waitFor(() => expect(apiGetMock).toHaveBeenCalledWith(expect.stringContaining("/tw/factor-validation?")));
    expect(apiGetMock).toHaveBeenCalledWith(expect.stringContaining("benchmark=taiex_total_return"));
    expect(apiGetMock).toHaveBeenCalledWith(expect.stringContaining("weight_mode=walk_forward"));
    expect(await screen.findByText("+15.00%")).toBeInTheDocument();
    expect(screen.getAllByText("+92.00%").length).toBeGreaterThan(0);
    expect(screen.getByText("Signal robustness")).toBeInTheDocument();
    expect(screen.getByText("Composite quintile returns")).toBeInTheDocument();
    expect(screen.getByText("Average factor rank correlation")).toBeInTheDocument();
    expect(screen.getByText("Holding-period sensitivity")).toBeInTheDocument();
    expect(screen.getByText("Top-N breadth sensitivity")).toBeInTheDocument();
    expect(screen.getByText("Walk-forward weight stability")).toBeInTheDocument();
    expect(screen.getByText("Factor IC decay")).toBeInTheDocument();
    expect(screen.getByText("Historical validation uses the current company universe")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Run & register challenger" }));
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith(
      "/tw/factor-research-runs",
      expect.objectContaining({ profile: "value", weight_mode: "walk_forward", auto_promote: false }),
    ));

    fireEvent.click(screen.getByRole("button", { name: "Build portfolio" }));
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith(
      "/tw/factor-portfolio",
      expect.objectContaining({ profile: "value", max_position_weight: .1, max_sector_weight: .3, current_weights: null }),
    ));
    expect(await screen.findByText("Target weight")).toBeInTheDocument();
    expect(screen.getByText("Annual volatility")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Source portfolio"), {
      target: { value: "11111111-1111-4111-8111-111111111111" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Preview rebalance" }));
    await waitFor(() => expect(apiPostMock).toHaveBeenCalledWith(
      "/tw/factor-portfolio/rebalance-preview",
      expect.objectContaining({
        portfolio_id: "11111111-1111-4111-8111-111111111111",
        profile: "value", allow_odd_lot: true, fee_bps: 14.25,
      }),
    ));
    expect(await screen.findByText("Buy")).toBeInTheDocument();
    expect(screen.getByText(/No transaction or order was created/)).toBeInTheDocument();
  });

  it("rejects malformed current weights before portfolio submission", async () => {
    renderPanel();
    await screen.findByText("2330");
    fireEvent.change(screen.getByLabelText(/Current weights/), {
      target: { value: "2330:80, 2317:30" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Build portfolio" }));
    expect(await screen.findByText("Portfolio construction could not be completed.")).toBeInTheDocument();
    expect(apiPostMock).not.toHaveBeenCalledWith("/tw/factor-portfolio", expect.anything());
  });
});
