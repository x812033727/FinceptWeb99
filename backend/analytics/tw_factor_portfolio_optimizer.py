"""Constrained, factor-score-driven TW portfolio construction."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from scipy.optimize import minimize


def _turnover(
    weights: np.ndarray, current: np.ndarray, external_current_weight: float = 0.0,
) -> float:
    current_cash = max(0.0, 1.0 - float(current.sum()) - external_current_weight)
    cash = max(0.0, 1.0 - float(weights.sum()))
    return 0.5 * (
        float(np.abs(weights - current).sum())
        + external_current_weight
        + abs(cash - current_cash)
    )


def _nearest_psd(matrix: np.ndarray) -> np.ndarray:
    symmetric = (matrix + matrix.T) / 2
    values, vectors = np.linalg.eigh(symmetric)
    return vectors @ np.diag(np.maximum(values, 1e-10)) @ vectors.T


def optimize_factor_portfolio(
    *, symbols: list[str], scores: list[float], industries: list[str],
    annual_covariance: np.ndarray, benchmark_covariance: np.ndarray,
    benchmark_variance: float, liquidity_caps: list[float],
    current_weights: dict[str, float] | None = None,
    max_position_weight: float = .10, max_sector_weight: float = .30,
    target_volatility: float = .20, max_tracking_error: float = .12,
    turnover_budget: float = .50, minimum_invested_weight: float = .80,
    risk_aversion: float = 2.0,
) -> dict[str, Any]:
    """Maximise factor utility subject to explicit execution/risk limits.

    No equal-weight fallback is returned on failure: a caller must be able to
    distinguish a genuinely feasible portfolio from a cosmetic suggestion.
    """
    n = len(symbols)
    if not n or not (
        len(scores) == len(industries) == len(liquidity_caps) == n
        and annual_covariance.shape == (n, n)
        and benchmark_covariance.shape == (n,)
    ):
        raise ValueError("portfolio optimizer inputs have inconsistent dimensions")
    if len(set(symbols)) != n:
        raise ValueError("portfolio optimizer symbols must be unique")
    covariance = _nearest_psd(np.asarray(annual_covariance, dtype=float))
    benchmark_covariance = np.asarray(benchmark_covariance, dtype=float)
    score_vector = np.clip(np.asarray(scores, dtype=float) / 100.0, 0, 1)
    caps = np.minimum(
        max_position_weight,
        np.clip(np.asarray(liquidity_caps, dtype=float), 0, max_position_weight),
    )
    current_map = current_weights or {}
    parsed_current = {symbol: float(value) for symbol, value in current_map.items()}
    if any(not np.isfinite(value) or value < 0 for value in parsed_current.values()):
        raise ValueError("current_weights must contain finite non-negative values")
    current = np.asarray([parsed_current.get(symbol, 0) for symbol in symbols])
    current_total = sum(parsed_current.values())
    external_current_weight = max(0.0, current_total - float(current.sum()))
    if current_total > 1 + 1e-9:
        raise ValueError("current_weights cannot sum above 1")

    sector_indices: dict[str, list[int]] = defaultdict(list)
    for index, industry in enumerate(industries):
        sector_indices[industry or "未分類"].append(index)

    def volatility(weights: np.ndarray) -> float:
        return float(np.sqrt(max(0.0, weights @ covariance @ weights)))

    def tracking_error(weights: np.ndarray) -> float:
        active_variance = (
            weights @ covariance @ weights
            + float(benchmark_variance)
            - 2 * weights @ benchmark_covariance
        )
        return float(np.sqrt(max(0.0, active_variance)))

    constraints: list[dict[str, Any]] = [
        {"type": "ineq", "fun": lambda w: 1.0 - float(w.sum())},
        {"type": "ineq", "fun": lambda w: float(w.sum()) - minimum_invested_weight},
        {"type": "ineq", "fun": lambda w: target_volatility - volatility(w)},
        {"type": "ineq", "fun": lambda w: max_tracking_error - tracking_error(w)},
    ]
    for indices in sector_indices.values():
        constraints.append({
            "type": "ineq",
            "fun": lambda w, idx=indices: max_sector_weight - float(w[idx].sum()),
        })
    if current_weights is not None:
        constraints.append({
            "type": "ineq",
            "fun": lambda w: turnover_budget
            - _turnover(w, current, external_current_weight),
        })

    def objective(weights: np.ndarray) -> float:
        risk = float(weights @ covariance @ weights)
        turnover_penalty = (
            _turnover(weights, current, external_current_weight) ** 2
            if current_weights is not None else 0
        )
        cash = 1 - float(weights.sum())
        return (
            -float(weights @ score_vector)
            + risk_aversion * risk
            + .05 * turnover_penalty
            + .02 * cash * cash
        )

    # Greedy score-aware starting point that respects hard caps and sectors.
    start = np.minimum(current, caps) if current_weights is not None else np.zeros(n)
    sector_used = {
        sector: float(start[indices].sum()) for sector, indices in sector_indices.items()
    }
    room = max(0.0, 1.0 - float(start.sum()))
    for index in np.argsort(-score_vector):
        sector = industries[index] or "未分類"
        add = min(caps[index] - start[index], max_sector_weight - sector_used[sector], room)
        if add > 0:
            start[index] += add
            sector_used[sector] += add
            room -= add
        if room <= 1e-12:
            break
    equal = np.minimum(caps, 1 / n)
    initial_points = [start, equal, np.minimum(caps, minimum_invested_weight / n)]
    results = [
        minimize(
            objective, initial, method="SLSQP", bounds=[(0.0, float(cap)) for cap in caps],
            constraints=constraints, options={"maxiter": 800, "ftol": 1e-10},
        )
        for initial in initial_points
    ]
    successful = [result for result in results if result.success]
    best = min(successful, key=lambda result: float(result.fun)) if successful else min(
        results, key=lambda result: float(result.fun),
    )
    weights = np.clip(np.asarray(best.x, dtype=float), 0, caps)
    sector_weights = {
        sector: float(weights[indices].sum()) for sector, indices in sector_indices.items()
    }
    invested = float(weights.sum())
    vol = volatility(weights)
    tracking = tracking_error(weights)
    turnover = (
        _turnover(weights, current, external_current_weight)
        if current_weights is not None else 0.0
    )
    violations = {
        "minimum_invested_weight": max(0.0, minimum_invested_weight - invested),
        "maximum_invested_weight": max(0.0, invested - 1),
        "target_volatility": max(0.0, vol - target_volatility),
        "maximum_tracking_error": max(0.0, tracking - max_tracking_error),
        "maximum_position_weight": max(0.0, float(np.max(weights - caps))),
        "maximum_sector_weight": max(
            [0.0, *(value - max_sector_weight for value in sector_weights.values())],
        ),
        "turnover_budget": (
            max(0.0, turnover - turnover_budget) if current_weights is not None else 0.0
        ),
    }
    feasible = bool(best.success) and max(violations.values()) <= 1e-5
    return {
        "converged": feasible,
        "solver_message": str(best.message),
        "weights": {
            symbol: round(float(weight), 8)
            for symbol, weight in zip(symbols, weights) if feasible and weight >= 1e-6
        },
        "summary": {
            "invested_weight": round(invested, 8),
            "cash_weight": round(max(0.0, 1 - invested), 8),
            "annual_volatility": round(vol, 8),
            "tracking_error": round(tracking, 8),
            "turnover": round(turnover, 8),
            "weighted_factor_score": round(float(weights @ score_vector) * 100, 4),
        },
        "sector_weights": {key: round(value, 8) for key, value in sector_weights.items()},
        "violations": {key: round(value, 10) for key, value in violations.items()},
        "caps": {symbol: round(float(cap), 8) for symbol, cap in zip(symbols, caps)},
    }
