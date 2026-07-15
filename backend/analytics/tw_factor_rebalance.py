"""Deterministic TW factor rebalance sizing and implementation-cost preview.

This module only produces a plan.  It never persists transactions or sends
orders, and deliberately keeps a funding shortfall visible instead of silently
shrinking the optimizer's target portfolio.
"""
from __future__ import annotations

import math
from typing import Any

from services.tw_symbol_service import is_etf

BOARD_LOT = 1_000


def _round_toward_zero(
    quantity: float, allow_odd_lot: bool, board_lot_size: int = BOARD_LOT,
) -> int:
    unit = 1 if allow_odd_lot else board_lot_size
    return int(abs(quantity) // unit) * unit


def _cost_for_trade(
    *, side: str, quantity: int, price: float, adv: float,
    fee_bps: float, minimum_fee_twd: float, stock_sell_tax_bps: float,
    etf_sell_tax_bps: float, slippage_bps: float,
    impact_coefficient_bps: float, max_impact_bps: float, symbol: str,
    sell_tax_bps_override: float | None = None,
) -> dict[str, float | bool]:
    mid_value = quantity * price
    liquidity_data_available = adv > 0
    participation = mid_value / adv if liquidity_data_available else 1.0
    impact_bps = (
        min(max_impact_bps, impact_coefficient_bps * math.sqrt(max(participation, 0.0)))
        if liquidity_data_available else max_impact_bps
    )
    implementation_bps = slippage_bps + impact_bps
    direction = 1 if side == "buy" else -1
    execution_price = price * (1 + direction * implementation_bps / 10_000)
    execution_value = quantity * execution_price
    fee = max(minimum_fee_twd, execution_value * fee_bps / 10_000)
    tax_bps = (
        sell_tax_bps_override
        if sell_tax_bps_override is not None
        else etf_sell_tax_bps if is_etf(symbol) else stock_sell_tax_bps
    ) if side == "sell" else 0.0
    tax = execution_value * tax_bps / 10_000
    shortfall = abs(execution_value - mid_value)
    return {
        "mid_value_twd": mid_value,
        "execution_price_twd": execution_price,
        "execution_value_twd": execution_value,
        "participation_rate": participation,
        "impact_bps": impact_bps,
        "fee_twd": fee,
        "tax_twd": tax,
        "implementation_shortfall_twd": shortfall,
        "total_cost_twd": fee + tax + shortfall,
        "liquidity_data_available": liquidity_data_available,
    }


def build_tw_factor_trades(
    *, target_positions: list[dict[str, Any]], current_positions: list[dict[str, Any]],
    portfolio_notional_twd: float, initial_cash_twd: float,
    allow_odd_lot: bool = True, min_trade_pct: float = .10,
    fee_bps: float = 14.25, minimum_fee_twd: float = 20,
    stock_sell_tax_bps: float = 30, etf_sell_tax_bps: float = 10,
    slippage_bps: float = 5, impact_coefficient_bps: float = 10,
    max_impact_bps: float = 100,
    sell_tax_bps_by_symbol: dict[str, float] | None = None,
    lot_size_by_symbol: dict[str, int] | None = None,
    trading_rule_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Turn target weights into rounded quantities and transparent costs."""
    if portfolio_notional_twd <= 0:
        raise ValueError("portfolio_notional_twd must be positive")
    numeric = {
        "initial_cash_twd": initial_cash_twd, "min_trade_pct": min_trade_pct,
        "fee_bps": fee_bps, "minimum_fee_twd": minimum_fee_twd,
        "stock_sell_tax_bps": stock_sell_tax_bps,
        "etf_sell_tax_bps": etf_sell_tax_bps, "slippage_bps": slippage_bps,
        "impact_coefficient_bps": impact_coefficient_bps,
        "max_impact_bps": max_impact_bps,
    }
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in numeric.values()):
        raise ValueError("rebalance cost and cash inputs must be finite and non-negative")
    tax_overrides = {
        str(symbol).strip().upper(): float(value)
        for symbol, value in (sell_tax_bps_by_symbol or {}).items()
    }
    if any(
        not symbol or not math.isfinite(value) or value < 0 or value > 100
        for symbol, value in tax_overrides.items()
    ):
        raise ValueError("sell tax overrides must be finite values between 0 and 100 bps")
    lot_sizes = {
        str(symbol).strip().upper(): int(value)
        for symbol, value in (lot_size_by_symbol or {}).items()
    }
    if any(not symbol or value < 1 for symbol, value in lot_sizes.items()):
        raise ValueError("board lot sizes must be positive integers")
    rule_metadata = trading_rule_by_symbol or {}

    targets = {str(row["symbol"]).upper(): row for row in target_positions}
    current = {str(row["symbol"]).upper(): row for row in current_positions}
    symbols = sorted(set(targets) | set(current))
    min_trade_value = portfolio_notional_twd * min_trade_pct / 100
    trades: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []

    for symbol in symbols:
        target = targets.get(symbol, {})
        holding = current.get(symbol, {})
        price = float(target.get("price") or holding.get("price") or 0)
        if price <= 0 or not math.isfinite(price):
            excluded.append({"symbol": symbol, "reason": "valid_price_unavailable"})
            continue
        current_qty = max(0.0, float(holding.get("quantity") or 0))
        target_weight = max(0.0, float(target.get("weight") or 0))
        target_value = target_weight * portfolio_notional_twd
        delta_value = target_value - current_qty * price
        if abs(delta_value) < min_trade_value:
            continue
        side = "buy" if delta_value > 0 else "sell"
        board_lot_size = lot_sizes.get(symbol, BOARD_LOT)
        quantity = _round_toward_zero(
            delta_value / price, allow_odd_lot, board_lot_size,
        )
        if side == "sell":
            quantity = min(quantity, int(current_qty))
        if quantity <= 0:
            excluded.append({"symbol": symbol, "reason": "below_selected_lot_size"})
            continue
        adv = max(0.0, float(target.get("average_daily_value_twd") or 0))
        cost = _cost_for_trade(
            side=side, quantity=quantity, price=price, adv=adv,
            fee_bps=fee_bps, minimum_fee_twd=minimum_fee_twd,
            stock_sell_tax_bps=stock_sell_tax_bps,
            etf_sell_tax_bps=etf_sell_tax_bps, slippage_bps=slippage_bps,
            impact_coefficient_bps=impact_coefficient_bps,
            max_impact_bps=max_impact_bps, symbol=symbol,
            sell_tax_bps_override=tax_overrides.get(symbol),
        )
        trades.append({
            "symbol": symbol, "side": side, "quantity": quantity,
            "mid_price_twd": round(price, 4),
            "execution_price_twd": round(cost["execution_price_twd"], 4),
            "gross_value_twd": round(cost["execution_value_twd"], 2),
            "fee_twd": round(cost["fee_twd"], 2),
            "tax_twd": round(cost["tax_twd"], 2),
            "implementation_shortfall_twd": round(cost["implementation_shortfall_twd"], 2),
            "total_cost_twd": round(cost["total_cost_twd"], 2),
            "impact_bps": round(cost["impact_bps"], 4),
            "participation_rate": round(cost["participation_rate"], 8),
            "liquidity_data_available": cost["liquidity_data_available"],
            "current_quantity": current_qty,
            "target_weight": round(target_weight, 8),
            "board_lot_size": board_lot_size,
            "sell_tax_bps": round(
                tax_overrides.get(
                    symbol,
                    etf_sell_tax_bps if is_etf(symbol) else stock_sell_tax_bps,
                ),
                4,
            ),
            "trading_rule_source": rule_metadata.get(symbol, {}).get(
                "source", "runtime_default",
            ),
            "tax_rule_code": rule_metadata.get(symbol, {}).get("tax_rule_code"),
        })

    trades.sort(key=lambda row: (row["side"] != "sell", -row["gross_value_twd"]))
    sell_proceeds = sum(
        row["gross_value_twd"] - row["fee_twd"] - row["tax_twd"]
        for row in trades if row["side"] == "sell"
    )
    buy_outflow = sum(
        row["gross_value_twd"] + row["fee_twd"]
        for row in trades if row["side"] == "buy"
    )
    ending_cash = initial_cash_twd + sell_proceeds - buy_outflow

    post_qty = {symbol: float(row.get("quantity") or 0) for symbol, row in current.items()}
    for row in trades:
        direction = 1 if row["side"] == "buy" else -1
        post_qty[row["symbol"]] = post_qty.get(row["symbol"], 0) + direction * row["quantity"]
    prices = {
        symbol: float(targets.get(symbol, {}).get("price") or current.get(symbol, {}).get("price") or 0)
        for symbol in symbols
    }
    post_market_value = sum(max(0, post_qty.get(symbol, 0)) * prices[symbol] for symbol in symbols)
    post_equity = post_market_value + ending_cash
    post_positions = [{
        "symbol": symbol,
        "quantity": post_qty[symbol],
        "market_value_twd": round(post_qty[symbol] * prices[symbol], 2),
        "actual_weight": round(post_qty[symbol] * prices[symbol] / post_equity, 8) if post_equity > 0 else 0,
        "target_weight": round(float(targets.get(symbol, {}).get("weight") or 0), 8),
        "weight_drift": round(
            (post_qty[symbol] * prices[symbol] / post_equity if post_equity > 0 else 0)
            - float(targets.get(symbol, {}).get("weight") or 0), 8,
        ),
    } for symbol in symbols if post_qty.get(symbol, 0) > 0 and prices[symbol] > 0]

    def scenario(name: str, multiplier: float) -> dict[str, Any]:
        variable = sum(
            row["implementation_shortfall_twd"] * multiplier + row["fee_twd"] + row["tax_twd"]
            for row in trades
        )
        return {"name": name, "multiplier": multiplier, "estimated_cost_twd": round(variable, 2)}

    total_cost = sum(row["total_cost_twd"] for row in trades)
    return {
        "trades": trades, "post_positions": post_positions, "excluded": excluded,
        "cost_scenarios": [scenario("low", .5), scenario("base", 1), scenario("stress", 2)],
        "summary": {
            "trade_count": len(trades),
            "gross_turnover_twd": round(sum(row["gross_value_twd"] for row in trades), 2),
            "estimated_total_cost_twd": round(total_cost, 2),
            "estimated_cost_bps": round(total_cost / portfolio_notional_twd * 10_000, 4),
            "initial_cash_twd": round(initial_cash_twd, 2),
            "ending_cash_twd": round(ending_cash, 2),
            "funding_shortfall_twd": round(max(0, -ending_cash), 2),
            "funded": ending_cash >= -0.01,
            "post_trade_equity_twd": round(post_equity, 2),
        },
        "methodology": {
            "rounding": "integer odd lots when enabled; otherwise effective-dated per-symbol board lots; always toward zero",
            "impact": "square-root participation model capped by max_impact_bps; missing ADV uses the cap",
            "tax": "sell-side only; effective-dated security-master rules take precedence over request and fallback defaults",
            "funding": "shortfalls remain visible; preview never rescales targets or executes orders",
        },
    }
