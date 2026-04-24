"""Unit tests for pure analytics modules — no DB/Redis/network needed."""
import pytest
import numpy as np
import pandas as pd

from analytics.dcf import run_dcf
from analytics.risk import var_historical, var_parametric, var_monte_carlo, portfolio_metrics
from analytics.backtest import run_backtest


# ── DCF ───────────────────────────────────────────────────────────

class TestDCF:
    def test_basic_output_keys(self):
        result = run_dcf(
            fcf_history=[100e9],
            growth_rate_1=0.10,
            growth_rate_2=0.05,
            terminal_growth=0.03,
            wacc=0.09,
            shares=15e9,
            net_debt=50e9,
        )
        assert "intrinsic_value" in result
        assert "equity_value" in result
        assert "pv_fcf" in result
        assert "pv_terminal" in result
        assert "sensitivity" in result
        assert "scenarios" in result

    def test_intrinsic_value_positive(self):
        result = run_dcf(
            fcf_history=[50e9],
            growth_rate_1=0.08,
            growth_rate_2=0.04,
            terminal_growth=0.025,
            wacc=0.08,
            shares=10e9,
            net_debt=0,
        )
        assert result["intrinsic_value"] > 0

    def test_margin_of_safety_negative_when_overvalued(self):
        result = run_dcf(
            fcf_history=[1e9],
            growth_rate_1=0.05,
            growth_rate_2=0.03,
            terminal_growth=0.02,
            wacc=0.10,
            shares=1e9,
            net_debt=0,
            current_price=9999.0,
        )
        assert result["margin_of_safety"] is not None
        assert result["margin_of_safety"] < 0

    def test_sensitivity_grid_shape(self):
        result = run_dcf(
            fcf_history=[100e9],
            growth_rate_1=0.10,
            growth_rate_2=0.05,
            terminal_growth=0.03,
            wacc=0.09,
            shares=15e9,
            net_debt=0,
        )
        grid = result["sensitivity"]["values"]
        assert len(grid) == 5        # 5 WACC rows
        assert len(grid[0]) == 3     # 3 terminal growth columns

    def test_scenarios_all_present(self):
        result = run_dcf(
            fcf_history=[100e9],
            growth_rate_1=0.10,
            growth_rate_2=0.05,
            terminal_growth=0.03,
            wacc=0.09,
            shares=15e9,
            net_debt=0,
        )
        assert set(result["scenarios"].keys()) >= {"bull", "base", "bear"}

    def test_bull_above_bear(self):
        result = run_dcf(
            fcf_history=[100e9],
            growth_rate_1=0.10,
            growth_rate_2=0.05,
            terminal_growth=0.03,
            wacc=0.09,
            shares=15e9,
            net_debt=0,
        )
        assert result["scenarios"]["bull"] > result["scenarios"]["bear"]

    def test_empty_fcf_returns_empty(self):
        result = run_dcf(
            fcf_history=[],
            growth_rate_1=0.10,
            growth_rate_2=0.05,
            terminal_growth=0.03,
            wacc=0.09,
            shares=15e9,
        )
        assert result == {}

    def test_wacc_exceeds_terminal_growth_required(self):
        # wacc == terminal_growth → terminal value should be 0 (safe clamp)
        result = run_dcf(
            fcf_history=[100e9],
            growth_rate_1=0.05,
            growth_rate_2=0.03,
            terminal_growth=0.09,
            wacc=0.09,
            shares=10e9,
            net_debt=0,
        )
        assert result["intrinsic_value"] >= 0


# ── VaR ───────────────────────────────────────────────────────────

class TestVaR:
    @pytest.fixture
    def returns(self):
        rng = np.random.default_rng(42)
        return rng.normal(0.0005, 0.015, 252)

    def test_historical_var_positive(self, returns):
        result = var_historical(returns, portfolio_value=100_000, confidence=0.95)
        assert result["var_pct"] > 0
        assert result["var_amount"] > 0

    def test_historical_cvar_geq_var(self, returns):
        result = var_historical(returns, portfolio_value=100_000, confidence=0.95)
        assert result["cvar_pct"] >= result["var_pct"]

    def test_parametric_var_positive(self, returns):
        result = var_parametric(returns, portfolio_value=100_000, confidence=0.95)
        assert result["var_pct"] > 0
        assert result["var_amount"] > 0

    def test_monte_carlo_var_positive(self):
        rng = np.random.default_rng(42)
        matrix = rng.normal(0.0005, 0.015, (252, 2))
        w = np.array([0.6, 0.4])
        result = var_monte_carlo(matrix, w, portfolio_value=100_000, confidence=0.95)
        assert result["var_pct"] > 0
        assert result["n_simulations"] == 10_000

    def test_horizon_scaling(self, returns):
        r1 = var_historical(returns, 100_000, 0.95, horizon_days=1)
        r5 = var_historical(returns, 100_000, 0.95, horizon_days=5)
        assert r5["var_pct"] > r1["var_pct"]

    def test_portfolio_metrics_keys(self, returns):
        result = portfolio_metrics(returns)
        assert "sharpe_ratio" in result
        assert "sortino_ratio" in result
        assert "max_drawdown" in result
        assert "calmar_ratio" in result
        assert "annualised_return" in result
        assert "annualised_volatility" in result

    def test_99_confidence_greater_than_95(self, returns):
        r95 = var_historical(returns, 100_000, 0.95)
        r99 = var_historical(returns, 100_000, 0.99)
        assert r99["var_pct"] >= r95["var_pct"]

    def test_insufficient_data_returns_empty(self):
        result = var_historical(np.array([0.01, -0.02]), 100_000, 0.95)
        assert result == {}

    def test_portfolio_metrics_max_drawdown_non_positive(self, returns):
        result = portfolio_metrics(returns)
        assert result["max_drawdown"] <= 0


# ── Backtest ──────────────────────────────────────────────────────

class TestBacktest:
    @pytest.fixture
    def price_df(self):
        rng = np.random.default_rng(42)
        dates = pd.date_range("2022-01-03", periods=252, freq="B").strftime("%Y-%m-%d")
        prices = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 252)))
        return pd.DataFrame({"AAPL": prices}, index=dates)

    def test_sma_crossover_runs(self, price_df):
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"], "fast": 10, "slow": 30})
        assert result["status"] == "completed"
        assert result["metrics"] is not None
        assert "total_return_pct" in result["metrics"]

    def test_rsi_strategy_runs(self, price_df):
        result = run_backtest(price_df, "rsi_mean_reversion", {"symbols": ["AAPL"]})
        assert result["status"] == "completed"

    def test_equity_curve_non_empty(self, price_df):
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"]})
        assert result["equity_curve"] and len(result["equity_curve"]) > 0

    def test_equity_curve_value_key(self, price_df):
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"]})
        assert "value" in result["equity_curve"][0]
        assert "date" in result["equity_curve"][0]

    def test_initial_capital_reflected(self, price_df):
        cap = 50_000.0
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"]}, initial_capital=cap)
        # First bar: strategy needs slow+1 bars before trading, so equity starts at cash = capital
        first_val = result["equity_curve"][0]["value"]
        assert abs(first_val - cap) < cap * 0.01

    def test_insufficient_data_returns_failed(self):
        tiny = pd.DataFrame({"AAPL": [100.0, 101.0, 99.0]}, index=["2022-01-03", "2022-01-04", "2022-01-05"])
        result = run_backtest(tiny, "sma_crossover", {"symbols": ["AAPL"]})
        assert result["status"] == "failed"

    def test_metrics_contain_expected_fields(self, price_df):
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"]})
        metrics = result["metrics"]
        for key in ("total_return_pct", "annualised_return_pct", "sharpe_ratio", "max_drawdown_pct"):
            assert key in metrics, f"missing metric: {key}"

    def test_trades_list_present(self, price_df):
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"], "fast": 10, "slow": 30})
        assert "trades" in result
        assert isinstance(result["trades"], list)
