"""
Portfolio optimizer — pure computation, no I/O.
Input:  returns_df  pd.DataFrame  columns=symbols, rows=dates, values=daily returns
        constraints dict          max_weight, min_weight, target_risk ("low"|"medium"|"high")
Output: weights     dict[str, float]   symbol → weight (sum to 1.0)
        metrics     dict               expected_return, volatility, sharpe
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from typing import Any


_RISK_PRESETS = {
    "low":    {"max_vol": 0.10},
    "medium": {"max_vol": 0.18},
    "high":   {"max_vol": 0.30},
}


def optimize(
    returns_df: pd.DataFrame,
    constraints: dict[str, Any] | None = None,
    risk_free_rate: float = 0.05,   # annualised
) -> dict[str, Any]:
    """
    Mean-variance optimisation (maximum Sharpe ratio).
    Falls back to equal weights if optimisation fails.
    """
    constraints = constraints or {}
    symbols = list(returns_df.columns)
    n = len(symbols)

    if n == 0:
        return {"weights": {}, "metrics": {}}
    if n == 1:
        return {
            "weights": {symbols[0]: 1.0},
            "metrics": _calc_metrics(returns_df, np.array([1.0]), risk_free_rate),
        }

    mu = returns_df.mean().values * 252          # annualised expected return
    cov = returns_df.cov().values * 252          # annualised covariance

    max_w = float(constraints.get("max_weight", 1.0))
    min_w = float(constraints.get("min_weight", 0.0))
    target_risk = constraints.get("target_risk", "medium")
    max_vol = _RISK_PRESETS.get(target_risk, _RISK_PRESETS["medium"])["max_vol"]

    bounds = [(min_w, max_w)] * n

    optim_constraints = [
        {"type": "eq", "fun": lambda w: np.sum(w) - 1.0},
        {"type": "ineq", "fun": lambda w: max_vol - float(np.sqrt(w @ cov @ w))},
    ]

    def neg_sharpe(w: np.ndarray) -> float:
        port_return = float(w @ mu)
        port_vol = float(np.sqrt(w @ cov @ w))
        if port_vol < 1e-9:
            return 0.0
        return -(port_return - risk_free_rate) / port_vol

    w0 = np.array([1.0 / n] * n)
    result = minimize(
        neg_sharpe,
        w0,
        method="SLSQP",
        bounds=bounds,
        constraints=optim_constraints,
        options={"maxiter": 500, "ftol": 1e-9},
    )

    weights_arr = result.x if result.success else w0
    # Clip and renormalise (handle numerical drift)
    weights_arr = np.clip(weights_arr, min_w, max_w)
    weights_arr /= weights_arr.sum()

    return {
        "weights": {sym: round(float(w), 6) for sym, w in zip(symbols, weights_arr)},
        "metrics": _calc_metrics(returns_df, weights_arr, risk_free_rate),
        "converged": result.success,
    }


def _calc_metrics(returns_df: pd.DataFrame, weights: np.ndarray, risk_free_rate: float) -> dict:
    mu = returns_df.mean().values * 252
    cov = returns_df.cov().values * 252
    port_return = float(weights @ mu)
    port_vol = float(np.sqrt(weights @ cov @ weights))
    sharpe = (port_return - risk_free_rate) / port_vol if port_vol > 1e-9 else 0.0

    # Max drawdown from cumulative return series
    cum = (1 + returns_df @ weights).cumprod()
    roll_max = cum.cummax()
    drawdown = (cum - roll_max) / roll_max
    max_dd = float(drawdown.min())

    return {
        "expected_annual_return": round(port_return, 4),
        "annual_volatility": round(port_vol, 4),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(max_dd, 4),
    }


def efficient_frontier(
    returns_df: pd.DataFrame,
    n_points: int = 30,
    risk_free_rate: float = 0.05,
) -> list[dict]:
    """Generate points on the efficient frontier for plotting."""
    symbols = list(returns_df.columns)
    n = len(symbols)
    if n < 2:
        return []

    mu = returns_df.mean().values * 252
    cov = returns_df.cov().values * 252
    min_vol = float(np.sqrt(np.diag(cov).min()))
    max_vol = float(np.sqrt(np.diag(cov).max()))

    frontier = []
    for target_vol in np.linspace(min_vol, max_vol, n_points):
        cons = [
            {"type": "eq", "fun": lambda w: np.sum(w) - 1.0},
            {"type": "ineq", "fun": lambda w: target_vol - float(np.sqrt(w @ cov @ w))},
        ]

        def neg_return(w):
            return -(float(w @ mu))

        res = minimize(
            neg_return,
            [1 / n] * n,
            method="SLSQP",
            bounds=[(0, 1)] * n,
            constraints=cons,
            options={"maxiter": 200},
        )
        if res.success:
            w = res.x
            frontier.append({
                "volatility": round(float(np.sqrt(w @ cov @ w)), 4),
                "return": round(float(w @ mu), 4),
            })

    return frontier
