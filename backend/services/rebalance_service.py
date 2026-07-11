"""Rebalance-plan builder (feature C5) — preview only, never places orders.

Reuses the existing pieces wholesale:
  - `get_portfolio_detail` for current holdings, live prices, weights and
    FX-converted values (portfolio currency),
  - `optimise_portfolio` for Markowitz target weights,
  - `_to_portfolio_currency` for the share-quantity conversion back into
    each holding's cost currency.

Design decisions (mirrored in the API docs):
  - Holdings the optimiser can't cover (insufficient history) are FROZEN,
    not sold: their current value is carved out and target weights are
    renormalised over the remaining "investable" slice. Selling a
    position because we lack data would be the worst possible default.
  - TW trades round to 1000-share board lots by default (odd-lot mode
    opt-in); US rounds to whole shares; crypto keeps 6 decimals.
  - Trades smaller than `min_trade_pct` of total value are dropped —
    a rebalance plan full of dust trades costs fees and adds nothing.
  - Sells are listed before buys (sells free the cash the buys need).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

_LOT_TW = 1000
_CRYPTO_DECIMALS = 6


def _round_qty(qty: float, market: str, allow_odd_lot: bool) -> float:
    if market == "TW" and not allow_odd_lot:
        lots = int(qty / _LOT_TW)          # toward zero — never overshoot
        return lots * _LOT_TW
    if market == "CRYPTO":
        return round(qty, _CRYPTO_DECIMALS)
    return float(int(qty))                 # US: whole shares, toward zero


async def build_rebalance_plan(
    portfolio_id: str,
    user_id: str,
    db: AsyncSession,
    *,
    target: str = "optimise",
    target_risk: str = "medium",
    max_weight: float = 1.0,
    custom_weights: dict[str, float] | None = None,
    fee_bps: float = 0.0,
    min_trade_pct: float = 1.0,
    allow_odd_lot: bool = False,
) -> dict[str, Any]:
    from services.portfolio_analytics import optimise_portfolio
    from services.portfolio_service import _to_portfolio_currency, get_portfolio_detail

    detail = await get_portfolio_detail(portfolio_id, user_id, db)
    holdings = detail["holdings"]
    total_value = detail["total_value"]
    if not holdings or not total_value:
        return {
            "portfolio_id": portfolio_id,
            "currency": detail["currency"],
            "total_value": total_value,
            "trades": [],
            "frozen": [],
            "summary": {"empty": True},
        }

    # ── Target weights ────────────────────────────────────────────
    if target == "custom":
        weights = dict(custom_weights or {})
        total_w = sum(weights.values())
        if not weights or abs(total_w - 1.0) > 0.02:
            raise ValueError("custom_weights must sum to 1.0")
        weights = {s: w / total_w for s, w in weights.items()}
    elif target == "equal_weight":
        weights = {h["symbol"]: 1.0 / len(holdings) for h in holdings}
    else:  # "optimise"
        result = await optimise_portfolio(
            portfolio_id, user_id, target_risk, max_weight, db,
        )
        weights = dict(result["weights"])

    # ── Freeze holdings the target has no opinion on ──────────────
    # (optimiser dropped them for insufficient history, or a custom
    # weight set simply didn't mention them).
    frozen = [h for h in holdings if h["symbol"] not in weights]
    frozen_value = sum(h["current_value"] for h in frozen)
    investable = total_value - frozen_value
    covered = [h for h in holdings if h["symbol"] in weights]
    by_symbol = {h["symbol"]: h for h in covered}

    # Renormalise over the investable slice (weights may not cover 100%
    # after the optimiser dropped someone).
    w_sum = sum(weights.values())
    weights = {s: w / w_sum for s, w in weights.items()} if w_sum else {}

    # ── Build trade list ──────────────────────────────────────────
    min_trade_value = total_value * (min_trade_pct / 100.0)
    trades: list[dict[str, Any]] = []
    for symbol, w in weights.items():
        h = by_symbol.get(symbol)
        if h is None:
            continue  # target references a symbol not held — out of scope for a preview
        target_value = investable * w
        delta_pc = target_value - h["current_value"]
        if abs(delta_pc) < min_trade_value:
            continue

        price = h["current_price"] or 0
        if price <= 0:
            continue  # degraded quote — refuse to size a trade off avg_cost
        # delta is in portfolio currency; price is in the holding's cost
        # currency. rate = cost→portfolio for one unit.
        rate = await _to_portfolio_currency(1.0, h["cost_currency"], detail["currency"])
        qty_raw = delta_pc / rate / price
        qty = _round_qty(abs(qty_raw), h["market"], allow_odd_lot)
        if qty <= 0:
            continue
        side = "buy" if delta_pc > 0 else "sell"
        if side == "sell":
            qty = min(qty, h["quantity"])   # can't sell what we don't hold
            if qty <= 0:
                continue
        trade_value_pc = qty * price * rate
        trades.append({
            "symbol": symbol,
            "market": h["market"],
            "side": side,
            "quantity": qty,
            "est_price": price,
            "price_currency": h["cost_currency"],
            "est_value": round(trade_value_pc, 2),
            "est_fee": round(trade_value_pc * fee_bps / 10_000.0, 2),
            "current_weight_pct": h["weight_pct"],
            "target_weight_pct": round(w * investable / total_value * 100, 2),
        })

    trades.sort(key=lambda t: (t["side"] != "sell", -t["est_value"]))

    sell_total = sum(t["est_value"] for t in trades if t["side"] == "sell")
    buy_total = sum(t["est_value"] for t in trades if t["side"] == "buy")
    fees = sum(t["est_fee"] for t in trades)
    return {
        "portfolio_id": portfolio_id,
        "currency": detail["currency"],
        "total_value": total_value,
        "target": target,
        "trades": trades,
        "frozen": [
            {"symbol": h["symbol"], "market": h["market"],
             "current_value": h["current_value"],
             "reason": "insufficient_history_or_unspecified"}
            for h in frozen
        ],
        "summary": {
            "sell_total": round(sell_total, 2),
            "buy_total": round(buy_total, 2),
            "est_fees": round(fees, 2),
            # Positive → plan leaves cash on the table (lot rounding +
            # dropped dust trades); negative → buys exceed sells and the
            # user needs external cash.
            "net_cash_flow": round(sell_total - buy_total - fees, 2),
            "trade_count": len(trades),
        },
    }
