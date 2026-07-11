import type { Market } from "./market";

export interface Holding {
  id: string;
  symbol: string;
  market: Market;
  quantity: number;
  avg_cost: number;
  cost_currency: string;
  current_price?: number;
  current_value?: number;
  unrealized_pnl?: number;
  unrealized_pnl_pct?: number;
  weight_pct?: number;
}

export interface Portfolio {
  id: string;
  name: string;
  currency: string;
  total_value?: number;
  total_pnl?: number;
  total_pnl_pct?: number;
  holdings: Holding[];
  created_at: string;
}

// ── Risk dashboard (feature C1) — GET /portfolio/{id}/risk ────────

export interface RiskVaREntry {
  method: "historical" | "parametric" | "monte_carlo";
  confidence_level: number;      // 0.95 | 0.99
  horizon_days: number;
  var_pct: number;               // fraction, e.g. 0.021 = 2.1%
  var_amount: number;            // in portfolio currency
  cvar_pct?: number | null;
}

export interface RiskMetrics {
  annualised_return: number;
  annualised_volatility: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  calmar_ratio: number;
  max_drawdown: number;          // negative fraction
  beta?: number | null;
  alpha?: number | null;
}

export interface RiskWeightRow {
  symbol: string;
  market: string;
  weight_pct: number;
  risk_contribution_pct?: number | null;
}

export interface RiskCorrelation {
  symbols: string[];
  matrix: number[][];            // symbols × symbols
}

export interface RiskWarning {
  kind: "single_position" | "market_bucket";
  key: string;
  weight_pct: number;
  threshold_pct: number;
}

export interface RiskExcludedHolding {
  symbol: string;
  market: string;
  reason: string;
  observations: number;
}

export interface PortfolioRisk {
  portfolio_id: string;
  currency: string;
  as_of: string;
  portfolio_value: number;
  observations: number;
  empty: boolean;
  benchmark?: string | null;
  metrics?: RiskMetrics | null;
  var: RiskVaREntry[];
  weights: RiskWeightRow[];
  correlation?: RiskCorrelation | null;
  warnings: RiskWarning[];
  excluded: RiskExcludedHolding[];
}

export interface Transaction {
  id: string;
  symbol: string;
  market: Market;
  tx_type: "buy" | "sell" | "dividend";
  quantity: number;
  price: number;
  fx_rate: number;
  tx_date: string;
  notes?: string;
}
