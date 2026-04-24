"""
Lightweight event-driven backtest engine — bar-by-bar, daily resolution.
Target: 5-year daily history for 10 assets in < 2 seconds.

Usage:
  result = run_backtest(prices_df, strategy_fn, params, initial_capital)

prices_df:   pd.DataFrame  index=dates (str), columns=symbols, values=close prices
strategy_fn: callable(context, bar) -> list[Order]
params:      dict passed to strategy_fn

Built-in strategies: "sma_crossover", "rsi_mean_reversion"
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Order:
    symbol: str
    side: str         # "buy" | "sell"
    quantity: float   # shares (fractional allowed)
    price: float | None = None   # None = use bar close


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_cost: float = 0.0


@dataclass
class Context:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    history: pd.DataFrame = field(default_factory=pd.DataFrame)
    current_date: str = ""

    def portfolio_value(self, prices: dict[str, float]) -> float:
        equity = sum(
            pos.quantity * prices.get(sym, pos.avg_cost)
            for sym, pos in self.positions.items()
        )
        return self.cash + equity


def run_backtest(
    prices_df: pd.DataFrame,
    strategy: str = "sma_crossover",
    params: dict | None = None,
    initial_capital: float = 100_000.0,
    commission: float = 0.001,   # 0.1% per trade
) -> dict[str, Any]:
    params = params or {}
    strategy_fn = _get_strategy(strategy)

    if prices_df.empty or len(prices_df) < 20:
        return {"status": "failed", "error": "Insufficient price data"}

    ctx = Context(cash=initial_capital, params=params)
    equity_curve: list[dict] = []
    trades: list[dict] = []

    for i, (date_str, row) in enumerate(prices_df.iterrows()):
        prices = row.dropna().to_dict()
        ctx.current_date = str(date_str)
        ctx.history = prices_df.iloc[: i + 1]

        # ── Strategy signal ───────────────────────────────────────
        try:
            orders = strategy_fn(ctx, prices)
        except Exception:
            orders = []

        # ── Fill orders ───────────────────────────────────────────
        for order in orders:
            fill_price = order.price or prices.get(order.symbol)
            if fill_price is None:
                continue
            cost = fill_price * order.quantity
            fee  = cost * commission

            if order.side == "buy":
                if ctx.cash < cost + fee:
                    continue
                ctx.cash -= cost + fee
                pos = ctx.positions.setdefault(order.symbol, Position(order.symbol))
                total_cost = pos.avg_cost * pos.quantity + fill_price * order.quantity
                pos.quantity += order.quantity
                pos.avg_cost = total_cost / pos.quantity if pos.quantity > 0 else 0
                trades.append({"date": date_str, "symbol": order.symbol, "side": "buy",
                                "quantity": order.quantity, "price": fill_price})

            elif order.side == "sell":
                pos = ctx.positions.get(order.symbol)
                if not pos or pos.quantity < order.quantity:
                    continue
                ctx.cash += cost - fee
                pos.quantity -= order.quantity
                if pos.quantity < 1e-9:
                    del ctx.positions[order.symbol]
                trades.append({"date": date_str, "symbol": order.symbol, "side": "sell",
                                "quantity": order.quantity, "price": fill_price})

        # ── Record equity ─────────────────────────────────────────
        equity_curve.append({
            "date":  str(date_str),
            "value": round(ctx.portfolio_value(prices), 2),
        })

    if not equity_curve:
        return {"status": "failed", "error": "No equity curve generated"}

    metrics = _compute_metrics(equity_curve, initial_capital, trades)
    return {
        "status": "completed",
        "equity_curve": equity_curve,
        "trades": trades[-200:],   # return last 200 trades to keep payload small
        "metrics": metrics,
    }


def _compute_metrics(equity_curve: list[dict], initial_capital: float, trades: list) -> dict:
    values = np.array([e["value"] for e in equity_curve], dtype=float)
    # Guard against zero-value bars (portfolio fully liquidated) which would
    # produce inf/NaN via plain division and corrupt every downstream metric.
    prev = values[:-1]
    returns = np.divide(
        np.diff(values), prev,
        out=np.zeros(len(prev), dtype=float),
        where=prev > 0,
    )

    total_return = (values[-1] - initial_capital) / initial_capital if initial_capital > 0 else 0.0
    n_days = len(values)
    ann_return = (1 + total_return) ** (252 / n_days) - 1 if n_days > 0 else 0

    ann_vol = float(returns.std()) * np.sqrt(252) if len(returns) > 1 else 0
    sharpe  = ann_return / ann_vol if ann_vol > 1e-9 else 0

    roll_max = np.maximum.accumulate(values)
    dd = np.divide(
        values - roll_max, roll_max,
        out=np.zeros_like(values),
        where=roll_max > 0,
    )
    max_dd = float(dd.min()) if len(dd) else 0.0

    buy_trades  = [t for t in trades if t["side"] == "buy"]
    sell_trades = [t for t in trades if t["side"] == "sell"]
    wins = sum(
        1 for s in sell_trades
        if s["price"] > next((b["price"] for b in reversed(buy_trades) if b["symbol"] == s["symbol"]), s["price"])
    )
    win_rate = wins / len(sell_trades) if sell_trades else 0.0

    return {
        "total_return_pct":      round(total_return * 100, 2),
        "annualised_return_pct": round(ann_return * 100, 2),
        "annualised_volatility": round(ann_vol * 100, 2),
        "sharpe_ratio":          round(sharpe, 4),
        "max_drawdown_pct":      round(max_dd * 100, 2),
        "win_rate":              round(win_rate, 4),
        "total_trades":          len(trades),
        "final_value":           round(float(values[-1]), 2),
    }


# ── Built-in strategies ───────────────────────────────────────────

def _get_strategy(name: str) -> Callable:
    return {"sma_crossover": _sma_crossover, "rsi_mean_reversion": _rsi_mean_reversion}.get(
        name, _sma_crossover
    )


def _sma_crossover(ctx: Context, prices: dict[str, float]) -> list[Order]:
    """
    Buy when fast SMA crosses above slow SMA; sell when it crosses below.
    params: fast (default 20), slow (default 50), symbol (default first col)
    """
    fast = ctx.params.get("fast", 20)
    slow = ctx.params.get("slow", 50)
    symbols = ctx.params.get("symbols") or list(prices.keys())[:1]

    orders: list[Order] = []
    if len(ctx.history) < slow + 1:
        return orders

    for sym in symbols:
        if sym not in ctx.history.columns:
            continue
        series = ctx.history[sym].dropna()
        if len(series) < slow:
            continue

        sma_fast = float(series.iloc[-fast:].mean())
        sma_slow = float(series.iloc[-slow:].mean())
        sma_fast_prev = float(series.iloc[-fast - 1:-1].mean())
        sma_slow_prev = float(series.iloc[-slow - 1:-1].mean())

        price = prices.get(sym, 0)
        pos = ctx.positions.get(sym)
        in_position = pos is not None and pos.quantity > 0

        # Golden cross → buy
        if sma_fast > sma_slow and sma_fast_prev <= sma_slow_prev and not in_position:
            qty = (ctx.cash * 0.95) / price if price > 0 else 0
            if qty > 0:
                orders.append(Order(sym, "buy", qty))

        # Death cross → sell all
        elif sma_fast < sma_slow and sma_fast_prev >= sma_slow_prev and in_position:
            orders.append(Order(sym, "sell", pos.quantity))

    return orders


def _rsi_mean_reversion(ctx: Context, prices: dict[str, float]) -> list[Order]:
    """
    Buy when RSI < oversold; sell when RSI > overbought.
    params: period (14), oversold (30), overbought (70), symbol
    """
    period    = ctx.params.get("period", 14)
    oversold  = ctx.params.get("oversold", 30)
    overbought = ctx.params.get("overbought", 70)
    symbols = ctx.params.get("symbols") or list(prices.keys())[:1]

    orders: list[Order] = []
    if len(ctx.history) < period + 2:
        return orders

    for sym in symbols:
        if sym not in ctx.history.columns:
            continue
        series = ctx.history[sym].dropna().iloc[-(period + 1):]
        if len(series) < period + 1:
            continue

        delta  = series.diff().dropna()
        gains  = delta.clip(lower=0).mean()
        losses = (-delta.clip(upper=0)).mean()
        rs     = gains / losses if losses > 1e-9 else 100
        rsi    = 100 - 100 / (1 + rs)

        price = prices.get(sym, 0)
        pos   = ctx.positions.get(sym)
        in_position = pos is not None and pos.quantity > 0

        if rsi < oversold and not in_position and price > 0:
            qty = (ctx.cash * 0.95) / price
            if qty > 0:
                orders.append(Order(sym, "buy", qty))
        elif rsi > overbought and in_position:
            orders.append(Order(sym, "sell", pos.quantity))

    return orders
