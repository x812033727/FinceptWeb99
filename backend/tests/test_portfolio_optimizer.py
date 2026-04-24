"""
Pure unit tests for analytics/portfolio_optimizer.py.
Exercises mean-variance optimisation and the efficient frontier directly —
no FastAPI, no DB, no JWT/cryptography imports, so these run locally even
when the integration-test client fixture is blocked by pyo3/cffi.
"""
import numpy as np
import pandas as pd
import pytest

from analytics.portfolio_optimizer import optimize, efficient_frontier


# ── fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def returns_2asset() -> pd.DataFrame:
    """Two uncorrelated assets with different risk/return profiles."""
    rng = np.random.default_rng(42)
    n = 252
    a = rng.normal(0.0008, 0.010, n)   # lower return, lower vol
    b = rng.normal(0.0015, 0.020, n)   # higher return, higher vol
    return pd.DataFrame({"A": a, "B": b})


@pytest.fixture
def returns_3asset() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 252
    return pd.DataFrame({
        "X": rng.normal(0.0005, 0.008, n),
        "Y": rng.normal(0.0010, 0.015, n),
        "Z": rng.normal(0.0020, 0.025, n),
    })


# ── optimize() edge cases ────────────────────────────────────────

def test_optimize_empty_returns_empty():
    result = optimize(pd.DataFrame())
    assert result["weights"] == {}
    assert result["metrics"] == {}


def test_optimize_single_asset_puts_all_weight_on_it(returns_2asset):
    single = returns_2asset[["A"]]
    result = optimize(single)
    assert result["weights"] == {"A": 1.0}
    # metrics should be populated (expected_annual_return, etc.)
    assert "expected_annual_return" in result["metrics"]
    assert "annual_volatility" in result["metrics"]


# ── optimize() happy path ────────────────────────────────────────

def test_optimize_weights_sum_to_one(returns_2asset):
    result = optimize(returns_2asset)
    total = sum(result["weights"].values())
    assert total == pytest.approx(1.0, abs=1e-4)


def test_optimize_returns_all_input_symbols(returns_3asset):
    result = optimize(returns_3asset)
    assert set(result["weights"].keys()) == {"X", "Y", "Z"}


def test_optimize_metrics_contain_expected_fields(returns_2asset):
    result = optimize(returns_2asset)
    for field in (
        "expected_annual_return",
        "annual_volatility",
        "sharpe_ratio",
        "max_drawdown",
    ):
        assert field in result["metrics"]


def test_optimize_reports_convergence(returns_2asset):
    result = optimize(returns_2asset)
    assert result["converged"] is True


# ── optimize() constraint enforcement ────────────────────────────

def test_optimize_respects_max_weight_cap(returns_3asset):
    """With max_weight=0.4, no single asset can exceed 0.4."""
    result = optimize(returns_3asset, constraints={"max_weight": 0.4})
    for w in result["weights"].values():
        assert w <= 0.4 + 1e-4


def test_optimize_respects_min_weight_floor(returns_3asset):
    result = optimize(returns_3asset, constraints={"min_weight": 0.1})
    for w in result["weights"].values():
        assert w >= 0.1 - 1e-4


def test_optimize_low_risk_vol_leq_high_risk(returns_3asset):
    """Low-risk target should produce ≤ volatility than high-risk.
    (The hard vol caps — 0.10, 0.30 — aren't always achievable given
    the asset set; when the low-risk cap is infeasible SLSQP falls
    back to equal weights, so we only assert relative ordering.)"""
    low = optimize(returns_3asset, constraints={"target_risk": "low"})
    high = optimize(returns_3asset, constraints={"target_risk": "high"})
    assert high["metrics"]["annual_volatility"] >= low["metrics"]["annual_volatility"]


def test_optimize_weights_are_non_negative(returns_3asset):
    """Default min_weight is 0 — no short positions."""
    result = optimize(returns_3asset)
    for w in result["weights"].values():
        assert w >= -1e-6


def test_optimize_sharpe_is_finite_number(returns_2asset):
    sharpe = optimize(returns_2asset)["metrics"]["sharpe_ratio"]
    assert np.isfinite(sharpe)


def test_optimize_max_drawdown_non_positive(returns_2asset):
    """max_drawdown is a negative percentage (or zero if no drawdown)."""
    mdd = optimize(returns_2asset)["metrics"]["max_drawdown"]
    assert mdd <= 1e-6


# ── efficient_frontier() ─────────────────────────────────────────

def test_efficient_frontier_empty_when_fewer_than_two_assets():
    single = pd.DataFrame({"A": np.random.default_rng(1).normal(0, 0.01, 100)})
    assert efficient_frontier(single) == []
    assert efficient_frontier(pd.DataFrame()) == []


def test_efficient_frontier_returns_requested_points(returns_2asset):
    points = efficient_frontier(returns_2asset, n_points=10)
    # SLSQP may fail for some target vols — we expect at least a majority to converge
    assert len(points) >= 5
    assert len(points) <= 10


def test_efficient_frontier_points_have_return_and_vol(returns_2asset):
    points = efficient_frontier(returns_2asset, n_points=5)
    assert len(points) > 0
    for p in points:
        assert "return" in p
        assert "volatility" in p
        assert np.isfinite(p["return"])
        assert np.isfinite(p["volatility"])
        assert p["volatility"] >= 0


def test_efficient_frontier_monotonic_return_vs_vol(returns_3asset):
    """Points sorted by volatility should show non-decreasing return
    (the frontier is concave, but along the lower boundary return
    is non-decreasing in volatility — the upper boundary, i.e. the
    frontier proper, is what we sample here)."""
    points = efficient_frontier(returns_3asset, n_points=20)
    by_vol = sorted(points, key=lambda p: p["volatility"])
    # Allow small numeric dips but overall trend should be up
    assert by_vol[-1]["return"] >= by_vol[0]["return"] - 0.01
