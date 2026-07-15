"""Owner-scoped factor portfolio rebalance preview orchestration."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from analytics.tw_factor_rebalance import build_tw_factor_trades
from services.portfolio_service import get_portfolio_detail
from services.tw_factor_portfolio_service import construct_factor_portfolio


async def build_factor_rebalance_preview(
    *, portfolio_id: str, user_id: str, db: AsyncSession,
    ranking: dict[str, Any], as_of: date, additional_cash_twd: float = 0,
    candidate_count: int = 30, max_position_weight: float = .10,
    max_sector_weight: float = .30, target_volatility: float = .20,
    max_tracking_error: float = .12, turnover_budget: float = .50,
    minimum_invested_weight: float = .80, max_participation_rate: float = .05,
    risk_aversion: float = 2, allow_odd_lot: bool = True,
    min_trade_pct: float = .10, fee_bps: float = 14.25,
    minimum_fee_twd: float = 20, stock_sell_tax_bps: float = 30,
    etf_sell_tax_bps: float = 10, slippage_bps: float = 5,
    impact_coefficient_bps: float = 10, max_impact_bps: float = 100,
    sell_tax_bps_by_symbol: dict[str, float] | None = None,
) -> dict[str, Any]:
    detail = await get_portfolio_detail(portfolio_id, user_id, db)
    tw_holdings = [row for row in detail["holdings"] if row["market"] == "TW"]
    frozen = [{
        "symbol": row["symbol"], "market": row["market"],
        "current_value": row["current_value"], "reason": "non_tw_holding_outside_factor_scope",
    } for row in detail["holdings"] if row["market"] != "TW"]
    current_positions: list[dict[str, Any]] = []
    current_value_twd = 0.0
    excluded_holdings: list[dict[str, str]] = []
    for row in tw_holdings:
        price = float(row.get("current_price") or 0)
        quantity = max(0.0, float(row.get("quantity") or 0))
        if price <= 0:
            excluded_holdings.append({
                "symbol": row["symbol"], "reason": "current_price_unavailable",
            })
            continue
        value = quantity * price
        current_value_twd += value
        current_positions.append({
            "symbol": row["symbol"], "quantity": quantity, "price": price,
        })
    cash_balances = dict(detail.get("cash_balances") or {})
    ledger_cash_twd = float(cash_balances.get("TWD", 0))
    initial_cash_twd = ledger_cash_twd + additional_cash_twd
    notional = current_value_twd + initial_cash_twd
    if notional < 100_000:
        raise ValueError("TW holdings plus additional_cash_twd must be at least TWD 100,000")
    current_weights = {
        row["symbol"]: row["quantity"] * row["price"] / notional
        for row in current_positions
    }
    target = await construct_factor_portfolio(
        ranking=ranking, as_of=as_of, candidate_count=candidate_count,
        portfolio_notional_twd=notional,
        max_position_weight=max_position_weight, max_sector_weight=max_sector_weight,
        target_volatility=target_volatility, max_tracking_error=max_tracking_error,
        turnover_budget=turnover_budget,
        minimum_invested_weight=minimum_invested_weight,
        max_participation_rate=max_participation_rate, risk_aversion=risk_aversion,
        current_weights=current_weights,
    )
    if target.get("converged") is False:
        trade_plan = {
            "trades": [], "excluded": [], "cost_scenarios": [
                {"name": name, "multiplier": multiplier, "estimated_cost_twd": 0}
                for name, multiplier in (("low", .5), ("base", 1), ("stress", 2))
            ],
            "post_positions": [{
                "symbol": row["symbol"], "quantity": row["quantity"],
                "market_value_twd": round(row["quantity"] * row["price"], 2),
                "actual_weight": round(row["quantity"] * row["price"] / notional, 8),
                "target_weight": None, "weight_drift": None,
            } for row in current_positions],
            "summary": {
                "trade_count": 0, "gross_turnover_twd": 0,
                "estimated_total_cost_twd": 0, "estimated_cost_bps": 0,
                "initial_cash_twd": round(initial_cash_twd, 2),
                "ending_cash_twd": round(initial_cash_twd, 2),
                "funding_shortfall_twd": 0, "funded": True,
                "post_trade_equity_twd": round(notional, 2),
            },
            "methodology": {
                "safety": "optimizer infeasibility produces zero trades; an empty target never means liquidate",
            },
        }
    else:
        from services.tw_security_master_service import resolve_security_profiles

        trade_symbols = {
            row["symbol"] for row in current_positions
        } | {
            row["symbol"] for row in target["positions"]
        }
        security_rules = await resolve_security_profiles(
            db, trade_symbols, as_of=as_of,
        )
        master_tax = {
            symbol: float(rule["sell_tax_bps"])
            for symbol, rule in security_rules.items()
        }
        # Request-level values are explicit scenario overrides and therefore
        # intentionally take precedence over the materialized master.
        master_tax.update(sell_tax_bps_by_symbol or {})
        trade_plan = build_tw_factor_trades(
            target_positions=target["positions"], current_positions=current_positions,
            portfolio_notional_twd=notional, initial_cash_twd=initial_cash_twd,
            allow_odd_lot=allow_odd_lot, min_trade_pct=min_trade_pct,
            fee_bps=fee_bps, minimum_fee_twd=minimum_fee_twd,
            stock_sell_tax_bps=stock_sell_tax_bps,
            etf_sell_tax_bps=etf_sell_tax_bps, slippage_bps=slippage_bps,
            impact_coefficient_bps=impact_coefficient_bps,
            max_impact_bps=max_impact_bps,
            sell_tax_bps_by_symbol=master_tax,
            lot_size_by_symbol={
                symbol: int(rule["board_lot_size"])
                for symbol, rule in security_rules.items()
            },
            trading_rule_by_symbol=security_rules,
        )
    trade_plan["excluded"] = excluded_holdings + trade_plan["excluded"]
    flags = list(target["quality"]["flags"])
    if frozen:
        flags.append("non_tw_holdings_frozen")
    if any(currency != "TWD" and abs(float(amount)) > 1e-6
           for currency, amount in cash_balances.items()):
        flags.append("foreign_currency_cash_frozen")
    if additional_cash_twd > 0:
        flags.append("hypothetical_additional_cash")
    if excluded_holdings:
        flags.append("tw_holdings_with_missing_prices_excluded")
    if not trade_plan["summary"]["funded"]:
        flags.append("funding_shortfall")
    if any(not row.get("liquidity_data_available", True) for row in trade_plan["trades"]):
        flags.append("missing_adv_uses_maximum_impact")
    if any(
        row.get("trading_rule_source") == "runtime_fallback"
        for row in trade_plan["trades"]
    ):
        flags.append("security_master_runtime_fallback")
    coverage = target.get("risk_comparison", {}).get("current_weight_coverage")
    if coverage is not None and coverage < .8:
        flags.append("low_pre_trade_risk_coverage")
    return {
        "portfolio_id": portfolio_id, "currency": "TWD",
        "portfolio_name": detail["name"], "portfolio_base_currency": detail["currency"],
        "portfolio_notional_twd": round(notional, 2),
        "ledger_cash_twd": round(ledger_cash_twd, 2),
        "additional_cash_twd": round(additional_cash_twd, 2),
        "target_portfolio": target, "trades": trade_plan["trades"],
        "post_positions": trade_plan["post_positions"],
        "cost_scenarios": trade_plan["cost_scenarios"],
        "frozen": frozen, "excluded": trade_plan["excluded"],
        "summary": trade_plan["summary"], "quality_flags": flags,
        "methodology": trade_plan["methodology"],
        "preview_only": True,
    }
