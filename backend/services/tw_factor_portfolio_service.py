"""Build a constrained, investable portfolio from TW factor rankings."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from analytics.tw_factor_portfolio_optimizer import optimize_factor_portfolio
from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from services.tw_factor_service import _load_research_sidecars, merge_adjusted_prices

PORTFOLIO_METHOD_VERSION = "tw-factor-portfolio-v1"
MIN_RETURN_OBSERVATIONS = 126
MAX_COVARIANCE_SESSIONS = 252


def _constraint(
    name: str, actual: float, limit: float, operator: str, *, tolerance: float = 1e-5,
) -> dict[str, Any]:
    passed = actual <= limit + tolerance if operator == "<=" else actual + tolerance >= limit
    return {
        "name": name, "actual": round(actual, 6), "limit": round(limit, 6),
        "operator": operator, "passed": passed,
        "binding": abs(actual - limit) <= max(tolerance, abs(limit) * .01),
    }


async def construct_factor_portfolio(
    *, ranking: dict[str, Any], as_of: date, candidate_count: int = 30,
    portfolio_notional_twd: float = 10_000_000,
    max_position_weight: float = .10, max_sector_weight: float = .30,
    target_volatility: float = .20, max_tracking_error: float = .12,
    turnover_budget: float = .50, minimum_invested_weight: float = .80,
    max_participation_rate: float = .05, risk_aversion: float = 2.0,
    current_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    if portfolio_notional_twd <= 0:
        raise ValueError("portfolio_notional_twd must be positive")
    candidates = list(ranking.get("candidates") or [])[:candidate_count]
    if len(candidates) < 5:
        raise ValueError("at least five ranked candidates are required")
    candidate_by_symbol = {str(row["symbol"]): row for row in candidates}
    symbols = list(candidate_by_symbol)
    start = as_of - timedelta(days=550)
    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(
            select(OhlcvDaily).where(
                OhlcvDaily.market == "TW",
                OhlcvDaily.symbol.in_([*symbols, "_TAIEX_TR"]),
                OhlcvDaily.ts >= start,
                OhlcvDaily.ts <= as_of,
                OhlcvDaily.close.isnot(None),
            ).order_by(OhlcvDaily.symbol.asc(), OhlcvDaily.ts.asc())
        )).all()
    raw_bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        raw_bars[row.symbol].append({
            "date": row.ts.isoformat(), "close": float(row.close),
            "raw_close": float(row.close),
            "volume": int(row.volume) if row.volume is not None else None,
        })
    benchmark_bars = raw_bars.pop("_TAIEX_TR", [])
    adjusted, _, _, _ = await _load_research_sidecars(start=start, end=as_of)
    bars = merge_adjusted_prices(dict(raw_bars), adjusted)

    def return_series(items: list[dict[str, Any]]) -> pd.Series:
        values = {
            str(item["date"])[:10]: float(item["close"])
            for item in items if item.get("date") and item.get("close")
        }
        series = pd.Series(values, dtype=float).sort_index()
        return series.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()

    benchmark = return_series(benchmark_bars).tail(MAX_COVARIANCE_SESSIONS)
    if len(benchmark) < MIN_RETURN_OBSERVATIONS:
        raise ValueError("TAIEX total-return history is insufficient for portfolio risk controls")
    market_dates = list(benchmark.index)
    eligible: list[str] = []
    returns: dict[str, pd.Series] = {}
    adv: dict[str, float] = {}
    excluded: list[dict[str, str]] = []
    for symbol in symbols:
        series = return_series(bars.get(symbol, [])).reindex(market_dates)
        observed = int(series.notna().sum())
        if observed < MIN_RETURN_OBSERVATIONS:
            excluded.append({"symbol": symbol, "reason": "insufficient_return_history"})
            continue
        recent = bars.get(symbol, [])[-20:]
        dollar_values = [
            float(row.get("raw_close") or row.get("close")) * float(row["volume"])
            for row in recent if row.get("volume") and (row.get("raw_close") or row.get("close"))
        ]
        average_daily_value = float(np.mean(dollar_values)) if len(dollar_values) >= 10 else 0
        liquidity_cap = average_daily_value * max_participation_rate / portfolio_notional_twd
        if liquidity_cap < .005:
            excluded.append({"symbol": symbol, "reason": "insufficient_liquidity_capacity"})
            continue
        eligible.append(symbol)
        # A missing close within an established listing represents no marked
        # move for covariance purposes; it is not forward-filled price data.
        returns[symbol] = series.fillna(0.0)
        adv[symbol] = average_daily_value
    if len(eligible) < 5:
        raise ValueError("fewer than five candidates have sufficient risk and liquidity history")
    frame = pd.DataFrame({symbol: returns[symbol] for symbol in eligible}, index=market_dates)
    benchmark_aligned = benchmark.reindex(market_dates).fillna(0.0)
    covariance = frame.cov().to_numpy(dtype=float) * 252
    benchmark_covariance = np.asarray([
        frame[symbol].cov(benchmark_aligned) * 252 for symbol in eligible
    ], dtype=float)
    benchmark_variance = float(benchmark_aligned.var() * 252)
    liquidity_caps = [
        min(max_position_weight, adv[symbol] * max_participation_rate / portfolio_notional_twd)
        for symbol in eligible
    ]
    normalized_current: dict[str, float] | None = None
    if current_weights is not None:
        normalized_current = defaultdict(float)
        for symbol, weight in current_weights.items():
            normalized_symbol = str(symbol).strip().upper()
            if not normalized_symbol:
                raise ValueError("current_weights symbols cannot be empty")
            normalized_current[normalized_symbol] += float(weight)
    optimized = optimize_factor_portfolio(
        symbols=eligible,
        scores=[float(candidate_by_symbol[symbol]["score"]) for symbol in eligible],
        industries=[str(candidate_by_symbol[symbol].get("industry") or "未分類") for symbol in eligible],
        annual_covariance=covariance,
        benchmark_covariance=benchmark_covariance,
        benchmark_variance=benchmark_variance,
        liquidity_caps=liquidity_caps,
        current_weights=normalized_current,
        max_position_weight=max_position_weight,
        max_sector_weight=max_sector_weight,
        target_volatility=target_volatility,
        max_tracking_error=max_tracking_error,
        turnover_budget=turnover_budget,
        minimum_invested_weight=minimum_invested_weight,
        risk_aversion=risk_aversion,
    )
    weights = optimized["weights"]
    weight_vector = np.asarray([float(weights.get(symbol, 0)) for symbol in eligible])
    variance = float(weight_vector @ covariance @ weight_vector)
    marginal = covariance @ weight_vector
    risk_contributions = (
        weight_vector * marginal / variance if variance > 1e-12 else np.zeros(len(eligible))
    )
    positions = [
        {
            "symbol": symbol,
            "name_zh": candidate_by_symbol[symbol].get("name_zh"),
            "industry": candidate_by_symbol[symbol].get("industry"),
            "price": candidate_by_symbol[symbol].get("price"),
            "weight": round(float(weights[symbol]), 8),
            "notional_twd": round(float(weights[symbol]) * portfolio_notional_twd, 2),
            "factor_score": float(candidate_by_symbol[symbol]["score"]),
            "liquidity_cap": optimized["caps"][symbol],
            "average_daily_value_twd": round(adv[symbol], 2),
            "risk_contribution": round(float(risk_contributions[index]), 8),
        }
        for index, symbol in enumerate(eligible) if symbol in weights
    ]
    summary = optimized["summary"]
    liquidity_utilization = max((
        float(weight) / max(float(optimized["caps"][symbol]), 1e-12)
        for symbol, weight in weights.items()
    ), default=0.0)
    constraints = [
        _constraint("minimum_invested_weight", summary["invested_weight"], minimum_invested_weight, ">="),
        _constraint("maximum_invested_weight", summary["invested_weight"], 1, "<="),
        _constraint("maximum_position_weight", max(weights.values(), default=0), max_position_weight, "<="),
        _constraint("maximum_sector_weight", max(optimized["sector_weights"].values(), default=0), max_sector_weight, "<="),
        _constraint("liquidity_capacity", liquidity_utilization, 1, "<="),
        _constraint("target_volatility", summary["annual_volatility"], target_volatility, "<="),
        _constraint("maximum_tracking_error", summary["tracking_error"], max_tracking_error, "<="),
        _constraint("turnover_budget", summary["turnover"], turnover_budget, "<="),
    ]
    adjusted_observations = sum(
        bool(row.get("adjusted")) for symbol in eligible for row in bars.get(symbol, [])
    )
    total_observations = sum(len(bars.get(symbol, [])) for symbol in eligible)
    adjusted_coverage = adjusted_observations / max(total_observations, 1) * 100
    quality_flags = (
        (["portfolio_optimizer_infeasible"] if not optimized["converged"] else [])
        + (["partial_risk_universe"] if excluded else [])
        + (["unadjusted_risk_history"] if adjusted_coverage == 0 else [])
        + (["partial_adjusted_risk_history"] if 0 < adjusted_coverage < 80 else [])
    )
    risk_comparison: dict[str, float | None] = {
        "pre_trade_annual_volatility": None,
        "post_trade_annual_volatility": summary["annual_volatility"],
        "pre_trade_tracking_error": None,
        "post_trade_tracking_error": summary["tracking_error"],
        "current_weight_coverage": None,
    }
    if normalized_current is not None:
        current_vector = np.asarray([
            float(normalized_current.get(symbol, 0)) for symbol in eligible
        ])
        current_variance = float(current_vector @ covariance @ current_vector)
        current_active_variance = (
            current_variance + benchmark_variance
            - 2 * current_vector @ benchmark_covariance
        )
        total_current = sum(normalized_current.values())
        risk_comparison.update({
            "pre_trade_annual_volatility": round(np.sqrt(max(0, current_variance)), 8),
            "pre_trade_tracking_error": round(np.sqrt(max(0, current_active_variance)), 8),
            "current_weight_coverage": round(
                float(current_vector.sum()) / max(total_current, 1e-12), 8,
            ),
        })
    return {
        "market": "TW", "as_of": as_of.isoformat(),
        "profile": ranking["profile"], "methodology_version": PORTFOLIO_METHOD_VERSION,
        "factor_methodology_version": ranking["methodology_version"],
        "weight_source": ranking.get("weight_source", "profile"),
        "model_id": ranking.get("model_id"),
        "converged": optimized["converged"],
        "solver_message": optimized["solver_message"],
        "positions": positions, "summary": summary,
        "risk_comparison": risk_comparison,
        "sector_weights": optimized["sector_weights"],
        "constraints": constraints,
        "quality": {
            "status": "good" if optimized["converged"] and not quality_flags else "degraded",
            "flags": quality_flags,
            "requested_candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible),
            "return_observations": len(market_dates),
            "excluded": excluded,
            "benchmark": "taiex_total_return",
            "adjusted_price_history_used": adjusted_coverage > 0,
            "adjusted_price_coverage_pct": round(adjusted_coverage, 1),
        },
        "methodology": {
            "objective": "maximise factor score utility with covariance risk and turnover penalties",
            "risk": "252-session annualised covariance; tracking error versus TAIEX total return",
            "execution": "20-session average traded value participation caps; unallocated capacity remains cash",
            "failure": "no equal-weight fallback; infeasible constraints return converged=false and no positions",
        },
    }
