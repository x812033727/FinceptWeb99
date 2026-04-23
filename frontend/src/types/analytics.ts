export interface DCFResult {
  intrinsic_value: number;
  current_price: number;
  margin_of_safety_pct: number;
  sensitivity: {
    wacc: number[];
    terminal_growth: number[];
    values: number[][];
  };
  scenario: {
    bull: number;
    base: number;
    bear: number;
  };
}

export interface VaRResult {
  method: "historical" | "parametric" | "monte_carlo";
  confidence_level: number;
  horizon_days: number;
  var_amount: number;
  var_pct: number;
  portfolio_value: number;
  sharpe_ratio?: number;
  sortino_ratio?: number;
  max_drawdown?: number;
}

export interface BacktestResult {
  task_id?: string;
  status: "pending" | "running" | "completed" | "failed";
  equity_curve?: { date: string; value: number }[];
  metrics?: {
    total_return_pct: number;
    annualized_return_pct: number;
    sharpe_ratio: number;
    max_drawdown_pct: number;
    win_rate: number;
    total_trades: number;
  };
}
