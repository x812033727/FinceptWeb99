"""
Risk metrics — pure NumPy/SciPy, no I/O.

Functions:
  var_historical      Historical simulation VaR
  var_parametric      Variance-covariance VaR
  var_monte_carlo     Monte Carlo VaR (Cholesky-correlated)
  portfolio_metrics   Sharpe, Sortino, Calmar, Beta, max drawdown
"""
import numpy as np
from scipy import stats
from typing import Any

TRADING_DAYS = 252


# ── VaR ───────────────────────────────────────────────────────────

def var_historical(
    returns: np.ndarray,
    portfolio_value: float,
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> dict[str, Any]:
    """Sort historical returns, take the (1-confidence) percentile."""
    if len(returns) < 20:
        return {}
    scaled = returns * np.sqrt(horizon_days)
    pct = np.percentile(scaled, (1 - confidence) * 100)
    var_pct = abs(float(pct))
    return {
        "method": "historical",
        "confidence_level": confidence,
        "horizon_days": horizon_days,
        "var_pct": round(var_pct, 6),
        "var_amount": round(var_pct * portfolio_value, 2),
        "cvar_pct": round(abs(float(scaled[scaled <= pct].mean())), 6) if (scaled <= pct).any() else var_pct,
    }


def var_parametric(
    returns: np.ndarray,
    portfolio_value: float,
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> dict[str, Any]:
    """Variance-covariance method — assumes normal distribution."""
    if len(returns) < 20:
        return {}
    mu  = float(returns.mean()) * TRADING_DAYS
    sig = float(returns.std())  * np.sqrt(TRADING_DAYS)
    z   = stats.norm.ppf(1 - confidence)
    # Daily VaR then scale to horizon
    daily_var = abs(z * float(returns.std()))
    var_pct   = daily_var * np.sqrt(horizon_days)
    return {
        "method": "parametric",
        "confidence_level": confidence,
        "horizon_days": horizon_days,
        "var_pct": round(var_pct, 6),
        "var_amount": round(var_pct * portfolio_value, 2),
        "annualised_return": round(mu, 4),
        "annualised_vol": round(sig, 4),
    }


def var_monte_carlo(
    returns_matrix: np.ndarray,
    weights: np.ndarray,
    portfolio_value: float,
    confidence: float = 0.95,
    horizon_days: int = 1,
    n_sims: int = 10_000,
) -> dict[str, Any]:
    """
    Monte Carlo simulation using Cholesky decomposition for correlation.
    returns_matrix: shape (T, n_assets)
    weights:        shape (n_assets,)
    """
    if returns_matrix.shape[0] < 30:
        return {}
    cov = np.cov(returns_matrix.T)
    if cov.ndim == 0:
        cov = np.array([[float(cov)]])
    try:
        L = np.linalg.cholesky(cov + np.eye(cov.shape[0]) * 1e-8)
    except np.linalg.LinAlgError:
        return {}

    rng = np.random.default_rng(42)
    z = rng.standard_normal((n_sims, returns_matrix.shape[1]))
    sim_returns = z @ L.T                        # correlated single-period returns
    port_returns = (sim_returns @ weights) * np.sqrt(horizon_days)
    pct = np.percentile(port_returns, (1 - confidence) * 100)
    var_pct = abs(float(pct))

    return {
        "method": "monte_carlo",
        "confidence_level": confidence,
        "horizon_days": horizon_days,
        "n_simulations": n_sims,
        "var_pct": round(var_pct, 6),
        "var_amount": round(var_pct * portfolio_value, 2),
        "cvar_pct": round(abs(float(port_returns[port_returns <= pct].mean())), 6)
            if (port_returns <= pct).any() else var_pct,
    }


# ── Portfolio metrics ─────────────────────────────────────────────

def portfolio_metrics(
    portfolio_returns: np.ndarray,
    benchmark_returns: np.ndarray | None = None,
    risk_free_rate: float = 0.05,
) -> dict[str, Any]:
    """
    Compute Sharpe, Sortino, Calmar, Beta, max drawdown.
    All inputs are daily returns arrays.
    """
    if len(portfolio_returns) < 20:
        return {}

    ann_ret  = float(portfolio_returns.mean()) * TRADING_DAYS
    ann_vol  = float(portfolio_returns.std())  * np.sqrt(TRADING_DAYS)

    # Sharpe
    sharpe = (ann_ret - risk_free_rate) / ann_vol if ann_vol > 1e-9 else 0.0

    # Sortino (downside deviation only)
    downside = portfolio_returns[portfolio_returns < 0]
    ann_downside = float(downside.std()) * np.sqrt(TRADING_DAYS) if len(downside) > 0 else ann_vol
    sortino = (ann_ret - risk_free_rate) / ann_downside if ann_downside > 1e-9 else 0.0

    # Max drawdown
    cum = (1 + portfolio_returns).cumprod()
    roll_max = np.maximum.accumulate(cum)
    dd = (cum - roll_max) / roll_max
    max_dd = float(dd.min())

    # Calmar
    calmar = ann_ret / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0

    # Beta vs benchmark
    beta = None
    alpha = None
    if benchmark_returns is not None and len(benchmark_returns) == len(portfolio_returns):
        cov_matrix = np.cov(portfolio_returns, benchmark_returns)
        bench_var  = float(np.var(benchmark_returns))
        beta  = float(cov_matrix[0, 1] / bench_var) if bench_var > 1e-9 else None
        bench_ann  = float(benchmark_returns.mean()) * TRADING_DAYS
        alpha = ann_ret - (risk_free_rate + (beta or 0) * (bench_ann - risk_free_rate))

    return {
        "annualised_return":     round(ann_ret, 4),
        "annualised_volatility": round(ann_vol, 4),
        "sharpe_ratio":          round(sharpe, 4),
        "sortino_ratio":         round(sortino, 4),
        "calmar_ratio":          round(calmar, 4),
        "max_drawdown":          round(max_dd, 4),
        "beta":                  round(beta, 4) if beta is not None else None,
        "alpha":                 round(alpha, 4) if alpha is not None else None,
    }
