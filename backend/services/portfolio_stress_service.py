"""Deterministic, explainable portfolio scenario stress testing.

This is decision support, not a forecast. Shocks are explicit assumptions and
are applied to the portfolio's current marked values without executing trades.
"""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.portfolio_service import get_portfolio_detail

SCENARIOS = {
    "taiex_drawdown": {"label": "TAIEX -10%", "tw": -0.10, "us": -0.03, "crypto": -0.05},
    "semiconductor_downturn": {"label": "Semiconductor factor -15%", "tw": -0.04, "us": -0.03, "crypto": -0.04},
    "twd_depreciation": {"label": "TWD/USD +5%", "tw": 0.0, "us": 0.0, "crypto": 0.0},
    "rates_up_100bp": {"label": "Rates +100bp", "tw": -0.04, "us": -0.07, "crypto": -0.10},
    "single_stock_gap": {"label": "Single-stock gap", "tw": 0.0, "us": 0.0, "crypto": 0.0},
}

_SEMICONDUCTORS = {
    "2330", "2303", "2454", "3711", "2379", "3034", "2408", "AAPL",
    "AMD", "AMAT", "ASML", "AVGO", "INTC", "KLAC", "LRCX", "MU", "NVDA", "QCOM", "TSM",
}


def _shock_for(
    scenario: str, holding: dict[str, Any], portfolio_currency: str,
    gap_symbol: str | None, gap_pct: float,
) -> tuple[float, list[str]]:
    market = str(holding["market"]).lower()
    symbol = str(holding["symbol"]).upper()
    shock = float(SCENARIOS[scenario].get(market, 0.0))
    drivers = [SCENARIOS[scenario]["label"]]

    if scenario == "semiconductor_downturn" and symbol in _SEMICONDUCTORS:
        shock = -0.15
        drivers.append("semiconductor classification")
    elif scenario == "twd_depreciation":
        # A 5% rise in TWD/USD benefits USD assets in a TWD portfolio;
        # its reciprocal is a 4.76% loss for TWD assets in a USD portfolio.
        if portfolio_currency == "TWD" and market in {"us", "crypto"}:
            shock = 0.05
        elif portfolio_currency == "USD" and market == "tw":
            shock = (1 / 1.05) - 1
        drivers.append("native currency translation")
    elif scenario == "single_stock_gap" and symbol == gap_symbol:
        shock = gap_pct / 100
        drivers.append("selected gap symbol")

    return shock, drivers


async def stress_test_portfolio(
    portfolio_id: str,
    user_id: str,
    db: AsyncSession,
    *,
    scenarios: list[str] | None = None,
    gap_symbol: str | None = None,
    gap_pct: float = -20.0,
) -> dict[str, Any]:
    detail = await get_portfolio_detail(portfolio_id, user_id, db)
    selected = scenarios or list(SCENARIOS)
    unknown = sorted(set(selected) - set(SCENARIOS))
    if unknown:
        raise ValueError(f"Unknown scenarios: {', '.join(unknown)}")

    holdings = detail["holdings"]
    total = float(detail["total_value"])
    if gap_symbol is None and holdings:
        gap_symbol = str(max(holdings, key=lambda row: float(row["current_value"]))["symbol"]).upper()
    else:
        gap_symbol = gap_symbol.upper() if gap_symbol else None

    results: list[dict[str, Any]] = []
    for scenario in selected:
        rows: list[dict[str, Any]] = []
        total_pnl = 0.0
        for holding in holdings:
            value = float(holding["current_value"])
            shock, drivers = _shock_for(scenario, holding, detail["currency"], gap_symbol, gap_pct)
            pnl = value * shock
            total_pnl += pnl
            rows.append({
                "symbol": holding["symbol"], "market": holding["market"],
                "current_value": round(value, 2), "shock_pct": round(shock * 100, 4),
                "pnl": round(pnl, 2), "drivers": drivers,
            })
        denominator = sum(abs(row["pnl"]) for row in rows)
        for row in rows:
            row["risk_contribution_pct"] = round(abs(row["pnl"]) / denominator * 100, 2) if denominator else 0.0

        post_value = total + total_pnl
        recommendations = []
        for row in rows:
            post_holding = row["current_value"] + row["pnl"]
            weight = post_holding / post_value * 100 if post_value > 0 else 0.0
            if weight > 35:
                reduce_by = max(0.0, post_holding - post_value * 0.30)
                recommendations.append({
                    "symbol": row["symbol"], "action": "review_reduce",
                    "current_stressed_weight_pct": round(weight, 2), "target_weight_pct": 30.0,
                    "indicative_amount": round(reduce_by, 2),
                    "reason": "stressed concentration exceeds 35%",
                })
        results.append({
            "scenario": scenario, "label": SCENARIOS[scenario]["label"],
            "pnl": round(total_pnl, 2), "pnl_pct": round(total_pnl / total * 100, 4) if total else 0.0,
            "post_scenario_value": round(post_value, 2), "holdings": rows,
            "rebalance_suggestions": recommendations,
        })

    return {
        "portfolio_id": portfolio_id, "currency": detail["currency"],
        "as_of": datetime.now(UTC).isoformat(), "valuation_source": "portfolio_current_valuation",
        "portfolio_value": round(total, 2), "gap_symbol": gap_symbol,
        "scenarios": results,
        "disclaimer": "Deterministic decision-support scenarios only; not a forecast or investment advice.",
    }
