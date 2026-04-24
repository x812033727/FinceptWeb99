"""Unit tests for pure analytics modules — no DB/Redis/network needed."""
import pytest
import numpy as np

from analytics.dcf import run_dcf
from analytics.risk import var_historical, var_parametric, var_monte_carlo, portfolio_metrics
from analytics.backtest import run_backtest

import pandas as pd


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
            current_price=9999.0,  # absurdly high
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
        assert len(grid) == 5         # 5 WACC rows
        assert len(grid[0]) == 3      # 3 terminal growth columns

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


# ── VaR ───────────────────────────────────────────────────────────

class TestVaR:
    @pytest.fixture
    def returns(self):
        rng = np.random.default_rng(42)
        return rng.normal(0.0005, 0.015, 252)

    def test_historical_var_positive(self, returns):
        result = var_historical(returns, portfolio_value=100_000, confidence=0.95)
        assert result["var"] > 0
        assert result["cvar"] >= result["var"]

    def test_parametric_var_positive(self, returns):
        result = var_parametric(returns, portfolio_value=100_000, confidence=0.95)
        assert result["var"] > 0

    def test_monte_carlo_var_positive(self):
        rng = np.random.default_rng(42)
        aligned = rng.normal(0.0005, 0.015, (252, 2))
        w = np.array([0.6, 0.4])
        result = var_monte_carlo(aligned, w, portfolio_value=100_000, confidence=0.95)
        assert result["var"] > 0

    def test_horizon_scaling(self, returns):
        r1 = var_historical(returns, 100_000, 0.95, horizon_days=1)
        r5 = var_historical(returns, 100_000, 0.95, horizon_days=5)
        # √5 scaling — 5-day VaR should be larger
        assert r5["var"] > r1["var"]

    def test_portfolio_metrics_keys(self, returns):
        metrics = portfolio_metrics(returns)
        assert "sharpe" in metrics
        assert "sortino" in metrics
        assert "max_drawdown" in metrics
        assert "calmar" in metrics

    def test_99_confidence_greater_than_95(self, returns):
        r95 = var_historical(returns, 100_000, 0.95)
        r99 = var_historical(returns, 100_000, 0.99)
        assert r99["var"] >= r95["var"]


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
        assert result["status"] == "ok"
        assert result["metrics"] is not None
        assert "total_return" in result["metrics"]

    def test_rsi_strategy_runs(self, price_df):
        result = run_backtest(price_df, "rsi_mean_reversion", {"symbols": ["AAPL"]})
        assert result["status"] == "ok"

    def test_equity_curve_non_empty(self, price_df):
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"]})
        assert result["equity_curve"] and len(result["equity_curve"]) > 0

    def test_initial_capital_respected(self, price_df):
        cap = 50_000.0
        result = run_backtest(price_df, "sma_crossover", {"symbols": ["AAPL"]}, initial_capital=cap)
        first_eq = result["equity_curve"][0]["equity"]
        assert abs(first_eq - cap) < cap * 0.01  # within 1% of initial capital

    def test_unknown_strategy_returns_failed(self, price_df):
        result = run_backtest(price_df, "nonexistent_strategy", {"symbols": ["AAPL"]})
        assert result["status"] == "failed"
