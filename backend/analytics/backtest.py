"""
Lightweight event-driven backtest engine — bar-by-bar, daily resolution.
Target: 5-year daily history for 10 assets in < 2 seconds.

Usage:
  result = run_backtest(prices_df, strategy, params, initial_capital)

prices_df:   pd.DataFrame  index=dates (str), columns=symbols, values=close prices
strategy:    registered strategy name (str) or callable(context, bar) -> list[Order]
params:      dict passed to the strategy

Built-in strategies (see STRATEGIES registry / list_strategies()):
  "sma_crossover", "rsi_mean_reversion", "breakout_n", "momentum",
  "bollinger_revert"

── Risk controls & fill model (C2 extension) ─────────────────────────
All risk-control arguments are optional and OFF by default; a call with
only the legacy arguments produces byte-identical output to the legacy
engine.

The engine sees exactly one price per bar (the daily close), so every
fill is derived from the bar close under a *conservative* fill model:

* Regular (strategy) orders fill at the bar close, adjusted by slippage.
* Risk exits are evaluated at the top of each bar — before that bar's
  strategy signals — against the bar close, with intra-bar priority
      stop_loss  →  trailing_stop  →  take_profit
  so when a single bar would trigger both a stop and a take-profit
  (a gap), the stop wins (conservative).
* Stop-loss / trailing-stop use stop-market semantics: the fill is
  never better than the trigger level. Long: fill = min(level, close)
  — a gap through the stop fills at the (worse) close. Short mirror:
  fill = max(level, close).
* Take-profit uses limit-order semantics: the fill is exactly the
  take-profit level (never the better close).
* Stop / take-profit levels are anchored at the position's average
  entry cost (`Position.avg_cost`); the trailing stop ratchets off the
  best close since entry (highest close for longs, lowest for shorts).
* Slippage (`slippage_bps`) worsens every fill in the direction of the
  trade: buys fill at price*(1 + bps/10⁴), sells at price*(1 - bps/10⁴).
* Commission: fee = fill_value * (commission + commission_bps/10⁴),
  charged on every fill, both directions.

── Position sizing ───────────────────────────────────────────────────
`position_size_pct` (0-1] resizes any order that OPENS a new position
to (portfolio equity × pct) / fill price; long entries are additionally
capped by available cash (incl. fees). Orders that add to or reduce an
existing position keep the strategy-specified quantity. Default (None):
the strategy's own quantity is always used — legacy behaviour.

── Short selling (margin-free simplified model) ──────────────────────
With `allow_short=False` (default) sells are capped at current holdings
(legacy behaviour). With `allow_short=True`, sell orders may exceed
holdings and open a negative position:

* short proceeds credit cash immediately; buys to cover debit cash;
* no borrow fee, no margin requirement, no forced liquidation;
* equity = cash + Σ qty·price — a negative qty naturally subtracts the
  buy-back liability, so short P&L is (entry − cover) × |qty| − costs;
* `avg_cost` on a short position is the average short-entry price; an
  order that flips the position sign resets `avg_cost` to its fill.
The flag is also injected into the strategy params (`allow_short`) so
built-in strategies can emit sell-to-open signals.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Any, Callable

_BPS = 1e-4


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
    # Best close since entry — trailing-stop watermarks (C2). Only
    # maintained when risk controls are active.
    high_water: float = 0.0
    low_water: float = 0.0


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
    strategy: str | Callable = "sma_crossover",
    params: dict | None = None,
    initial_capital: float = 100_000.0,
    commission: float = 0.001,   # 0.1% per trade (legacy flat rate)
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    position_size_pct: float | None = None,
    slippage_bps: float = 0.0,
    commission_bps: float = 0.0,
    allow_short: bool = False,
) -> dict[str, Any]:
    params = dict(params or {})
    strategy_fn = strategy if callable(strategy) else _get_strategy(strategy)

    # "Extended" mode = any C2 feature enabled. Output enrichment
    # (exit_reason on trades, cost summary in metrics) only happens in
    # extended mode so a legacy-config run stays byte-identical.
    has_risk = any(
        v is not None and v > 0
        for v in (stop_loss_pct, take_profit_pct, trailing_stop_pct)
    )
    extended = (
        has_risk or allow_short or position_size_pct is not None
        or slippage_bps > 0 or commission_bps > 0
    )
    fee_rate = commission + commission_bps * _BPS
    slip = slippage_bps * _BPS

    if allow_short:
        params["allow_short"] = True

    if prices_df.empty or len(prices_df) < 20:
        return {"status": "failed", "error": "Insufficient price data"}

    ctx = Context(cash=initial_capital, params=params)
    equity_curve: list[dict] = []
    trades: list[dict] = []
    total_commission = 0.0
    total_slippage = 0.0

    def fill(order: Order, close_prices: dict[str, float],
             date_str: Any, exit_reason: str | None = None) -> None:
        """Execute one order against the current bar. Mutates ctx /
        trades / cost accumulators. Conservative slippage: buys fill
        higher, sells fill lower than the raw price."""
        nonlocal total_commission, total_slippage
        raw_price = order.price if order.price else close_prices.get(order.symbol)
        if raw_price is None or raw_price <= 0 or order.quantity <= 0:
            return
        pos = ctx.positions.get(order.symbol)
        held = pos.quantity if pos else 0.0

        if order.side == "buy":
            fill_price = raw_price * (1 + slip) if slip else raw_price
            quantity = order.quantity
            # Position sizing: only orders opening a NEW position are
            # resized; longs additionally capped by available cash.
            if position_size_pct is not None and held == 0.0:
                equity = ctx.portfolio_value(close_prices)
                quantity = (equity * position_size_pct) / fill_price
                affordable = ctx.cash / (fill_price * (1 + fee_rate))
                quantity = min(quantity, affordable)
                if quantity <= 0:
                    return
            cost = fill_price * quantity
            fee = cost * fee_rate
            if ctx.cash < cost + fee:
                return
            ctx.cash -= cost + fee
            pos = ctx.positions.setdefault(order.symbol, Position(order.symbol))
            prev_qty = pos.quantity
            new_qty = prev_qty + quantity
            if prev_qty >= 0:
                # Plain long open / add: weighted-average entry cost.
                total_cost = pos.avg_cost * prev_qty + fill_price * quantity
                pos.avg_cost = total_cost / new_qty if new_qty > 0 else 0
            elif new_qty > 0:
                # Short flipped to long: entry resets at the flip fill.
                pos.avg_cost = fill_price
                pos.high_water = close_prices.get(order.symbol, fill_price)
            # Partial short cover (new_qty < 0): avg_cost unchanged.
            pos.quantity = new_qty
            if prev_qty == 0:
                pos.high_water = close_prices.get(order.symbol, fill_price)
            if abs(pos.quantity) < 1e-9:
                del ctx.positions[order.symbol]
            trade = {"date": date_str, "symbol": order.symbol, "side": "buy",
                     "quantity": quantity, "price": fill_price}
            if extended:
                slip_cost = (fill_price - raw_price) * quantity
                total_commission += fee
                total_slippage += slip_cost
                trade["commission"] = round(fee, 4)
                trade["slippage"] = round(slip_cost, 4)
                if prev_qty < 0:   # covering a short = closing trade
                    trade["exit_reason"] = exit_reason or "signal"
            trades.append(trade)

        elif order.side == "sell":
            if not allow_short and (not pos or pos.quantity < order.quantity):
                return
            fill_price = raw_price * (1 - slip) if slip else raw_price
            quantity = order.quantity
            if position_size_pct is not None and held == 0.0:
                # Sell-to-open sizing (short entry): equity fraction.
                equity = ctx.portfolio_value(close_prices)
                quantity = (equity * position_size_pct) / fill_price
                if quantity <= 0:
                    return
            proceeds = fill_price * quantity
            fee = proceeds * fee_rate
            ctx.cash += proceeds - fee
            pos = ctx.positions.setdefault(order.symbol, Position(order.symbol))
            prev_qty = pos.quantity
            new_qty = prev_qty - quantity
            if prev_qty <= 0:
                # Short open / extend: weighted-average short entry.
                total_cost = pos.avg_cost * (-prev_qty) + fill_price * quantity
                pos.avg_cost = total_cost / (-new_qty) if new_qty < 0 else 0
            elif new_qty < 0:
                # Long flipped to short: entry resets at the flip fill.
                pos.avg_cost = fill_price
                pos.low_water = close_prices.get(order.symbol, fill_price)
            # Long reduce / close (new_qty >= 0): avg_cost unchanged.
            pos.quantity = new_qty
            if prev_qty == 0:
                pos.low_water = close_prices.get(order.symbol, fill_price)
            if abs(pos.quantity) < 1e-9:
                del ctx.positions[order.symbol]
            trade = {"date": date_str, "symbol": order.symbol, "side": "sell",
                     "quantity": quantity, "price": fill_price}
            if extended:
                slip_cost = (raw_price - fill_price) * quantity
                total_commission += fee
                total_slippage += slip_cost
                trade["commission"] = round(fee, 4)
                trade["slippage"] = round(slip_cost, 4)
                if prev_qty > 0:   # reducing a long = closing trade
                    trade["exit_reason"] = exit_reason or "signal"
            trades.append(trade)

    for i, (date_str, row) in enumerate(prices_df.iterrows()):
        prices = row.dropna().to_dict()
        ctx.current_date = str(date_str)
        ctx.history = prices_df.iloc[: i + 1]

        # ── Risk exits (before this bar's strategy signals) ───────
        if has_risk:
            for order, reason in _risk_exits(
                ctx, prices,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                trailing_stop_pct=trailing_stop_pct,
            ):
                fill(order, prices, date_str, exit_reason=reason)

        # ── Strategy signal ───────────────────────────────────────
        try:
            orders = strategy_fn(ctx, prices)
        except Exception:
            orders = []

        # ── Fill orders ───────────────────────────────────────────
        for order in orders:
            fill(order, prices, date_str)

        # ── Record equity ─────────────────────────────────────────
        equity_curve.append({
            "date":  str(date_str),
            "value": round(ctx.portfolio_value(prices), 2),
        })

    if not equity_curve:
        return {"status": "failed", "error": "No equity curve generated"}

    metrics = _compute_metrics(equity_curve, initial_capital, trades)
    if extended:
        metrics["total_commission"] = round(total_commission, 2)
        metrics["total_slippage"] = round(total_slippage, 2)
    return {
        "status": "completed",
        "equity_curve": equity_curve,
        "trades": trades[-200:],   # return last 200 trades to keep payload small
        "metrics": metrics,
    }


def _risk_exits(
    ctx: Context,
    prices: dict[str, float],
    *,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    trailing_stop_pct: float | None,
) -> list[tuple[Order, str]]:
    """Evaluate stop-loss / trailing-stop / take-profit for every open
    position against the current bar close. Returns (order, reason)
    pairs; the order's `price` carries the conservative fill level (see
    module docstring for the fill model). Also ratchets the trailing
    watermarks."""
    exits: list[tuple[Order, str]] = []
    for sym, pos in list(ctx.positions.items()):
        close = prices.get(sym)
        if close is None or close <= 0 or pos.quantity == 0:
            continue
        entry = pos.avg_cost
        if pos.quantity > 0:
            pos.high_water = max(pos.high_water or close, close)
            if stop_loss_pct and entry > 0:
                level = entry * (1 - stop_loss_pct)
                if close <= level:
                    exits.append((Order(sym, "sell", pos.quantity,
                                        price=min(level, close)), "stop"))
                    continue
            if trailing_stop_pct and pos.high_water > 0:
                level = pos.high_water * (1 - trailing_stop_pct)
                if close <= level:
                    exits.append((Order(sym, "sell", pos.quantity,
                                        price=min(level, close)), "trailing"))
                    continue
            if take_profit_pct and entry > 0:
                level = entry * (1 + take_profit_pct)
                if close >= level:
                    exits.append((Order(sym, "sell", pos.quantity,
                                        price=level), "take_profit"))
        else:
            qty = -pos.quantity
            pos.low_water = min(pos.low_water or close, close)
            if stop_loss_pct and entry > 0:
                level = entry * (1 + stop_loss_pct)
                if close >= level:
                    exits.append((Order(sym, "buy", qty,
                                        price=max(level, close)), "stop"))
                    continue
            if trailing_stop_pct and pos.low_water > 0:
                level = pos.low_water * (1 + trailing_stop_pct)
                if close >= level:
                    exits.append((Order(sym, "buy", qty,
                                        price=max(level, close)), "trailing"))
                    continue
            if take_profit_pct and entry > 0:
                level = entry * (1 - take_profit_pct)
                if close <= level:
                    exits.append((Order(sym, "buy", qty,
                                        price=level), "take_profit"))
    return exits


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


# ── Strategy registry & param schemas ─────────────────────────────

@dataclass(frozen=True)
class ParamSpec:
    """Schema entry for one strategy parameter — introspectable by the
    frontend form builder and by sweep tooling."""
    name: str
    type: str                    # "int" | "float"
    default: float
    min: float | None = None
    max: float | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "type": self.type, "default": self.default,
            "min": self.min, "max": self.max, "description": self.description,
        }


@dataclass(frozen=True)
class StrategySpec:
    name: str
    fn: Callable
    label: str
    description: str
    params: tuple[ParamSpec, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "label": self.label,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }


def _get_strategy(name: str) -> Callable:
    spec = STRATEGIES.get(name)
    return spec.fn if spec else _sma_crossover


def list_strategies() -> list[dict[str, Any]]:
    """Serializable registry view for the /backtest/strategies API."""
    return [spec.to_dict() for spec in STRATEGIES.values()]


# ── Built-in strategies ───────────────────────────────────────────

def _entry_qty(ctx: Context, prices: dict[str, float], price: float) -> float:
    """Default entry sizing shared by the built-ins: 95% of available
    cash (both long and sell-to-open entries; entering from flat means
    cash ≈ equity). Engine-level `position_size_pct` overrides this."""
    return (ctx.cash * 0.95) / price if price > 0 else 0.0


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


def _breakout_n(ctx: Context, prices: dict[str, float]) -> list[Order]:
    """
    N-day high/low breakout (Donchian-style). Long when today's close
    exceeds the highest close of the previous `entry_n` bars; exit the
    long when close drops below the lowest close of the previous
    `exit_n` bars. With allow_short, mirror on the downside: short on
    an `entry_n`-low breakdown, cover on an `exit_n`-high breakout.
    params: entry_n (20), exit_n (10)
    """
    entry_n = int(ctx.params.get("entry_n", 20))
    exit_n  = int(ctx.params.get("exit_n", 10))
    allow_short = bool(ctx.params.get("allow_short"))
    symbols = ctx.params.get("symbols") or list(prices.keys())[:1]

    orders: list[Order] = []
    if len(ctx.history) < entry_n + 1:
        return orders

    for sym in symbols:
        if sym not in ctx.history.columns:
            continue
        series = ctx.history[sym].dropna()
        if len(series) < entry_n + 1:
            continue
        close = prices.get(sym, 0)
        if close <= 0:
            continue
        entry_window = series.iloc[-(entry_n + 1):-1]   # previous N closes
        exit_window  = series.iloc[-(exit_n + 1):-1]

        pos = ctx.positions.get(sym)
        qty_held = pos.quantity if pos else 0.0

        if qty_held == 0:
            if close > float(entry_window.max()):
                qty = _entry_qty(ctx, prices, close)
                if qty > 0:
                    orders.append(Order(sym, "buy", qty))
            elif allow_short and close < float(entry_window.min()):
                qty = _entry_qty(ctx, prices, close)
                if qty > 0:
                    orders.append(Order(sym, "sell", qty))
        elif qty_held > 0 and close < float(exit_window.min()):
            orders.append(Order(sym, "sell", qty_held))
        elif qty_held < 0 and close > float(exit_window.max()):
            orders.append(Order(sym, "buy", -qty_held))

    return orders


def _momentum(ctx: Context, prices: dict[str, float]) -> list[Order]:
    """
    Time-series momentum. Long when the `lookback`-day return exceeds
    `threshold`; exit when the momentum turns negative. With
    allow_short, short when the return drops below -`threshold` and
    cover when momentum turns positive.
    params: lookback (20), threshold (0.05)
    """
    lookback  = int(ctx.params.get("lookback", 20))
    threshold = float(ctx.params.get("threshold", 0.05))
    allow_short = bool(ctx.params.get("allow_short"))
    symbols = ctx.params.get("symbols") or list(prices.keys())[:1]

    orders: list[Order] = []
    if len(ctx.history) < lookback + 1:
        return orders

    for sym in symbols:
        if sym not in ctx.history.columns:
            continue
        series = ctx.history[sym].dropna()
        if len(series) < lookback + 1:
            continue
        close = prices.get(sym, 0)
        base = float(series.iloc[-(lookback + 1)])
        if close <= 0 or base <= 0:
            continue
        ret = close / base - 1

        pos = ctx.positions.get(sym)
        qty_held = pos.quantity if pos else 0.0

        if qty_held == 0:
            if ret > threshold:
                qty = _entry_qty(ctx, prices, close)
                if qty > 0:
                    orders.append(Order(sym, "buy", qty))
            elif allow_short and ret < -threshold:
                qty = _entry_qty(ctx, prices, close)
                if qty > 0:
                    orders.append(Order(sym, "sell", qty))
        elif qty_held > 0 and ret < 0:
            orders.append(Order(sym, "sell", qty_held))
        elif qty_held < 0 and ret > 0:
            orders.append(Order(sym, "buy", -qty_held))

    return orders


def _bollinger_revert(ctx: Context, prices: dict[str, float]) -> list[Order]:
    """
    Bollinger-band mean reversion. Long when the close drops below the
    lower band (mean - num_std·σ over `period` bars incl. today); exit
    when the close reverts to/above the mean. With allow_short, short
    above the upper band and cover at/below the mean.
    params: period (20), num_std (2.0)
    """
    period  = int(ctx.params.get("period", 20))
    num_std = float(ctx.params.get("num_std", 2.0))
    allow_short = bool(ctx.params.get("allow_short"))
    symbols = ctx.params.get("symbols") or list(prices.keys())[:1]

    orders: list[Order] = []
    if len(ctx.history) < period:
        return orders

    for sym in symbols:
        if sym not in ctx.history.columns:
            continue
        series = ctx.history[sym].dropna().iloc[-period:]
        if len(series) < period:
            continue
        close = prices.get(sym, 0)
        if close <= 0:
            continue
        mean = float(series.mean())
        std  = float(series.std(ddof=0))
        lower = mean - num_std * std
        upper = mean + num_std * std

        pos = ctx.positions.get(sym)
        qty_held = pos.quantity if pos else 0.0

        if qty_held == 0 and std > 1e-12:
            if close < lower:
                qty = _entry_qty(ctx, prices, close)
                if qty > 0:
                    orders.append(Order(sym, "buy", qty))
            elif allow_short and close > upper:
                qty = _entry_qty(ctx, prices, close)
                if qty > 0:
                    orders.append(Order(sym, "sell", qty))
        elif qty_held > 0 and close >= mean:
            orders.append(Order(sym, "sell", qty_held))
        elif qty_held < 0 and close <= mean:
            orders.append(Order(sym, "buy", -qty_held))

    return orders


STRATEGIES: dict[str, StrategySpec] = {
    "sma_crossover": StrategySpec(
        name="sma_crossover", fn=_sma_crossover,
        label="SMA Crossover",
        description="Golden/death cross of fast vs slow simple moving averages.",
        params=(
            ParamSpec("fast", "int", 20, 2, 200, "Fast SMA window (bars)"),
            ParamSpec("slow", "int", 50, 5, 400, "Slow SMA window (bars)"),
        ),
    ),
    "rsi_mean_reversion": StrategySpec(
        name="rsi_mean_reversion", fn=_rsi_mean_reversion,
        label="RSI Mean Reversion",
        description="Buy oversold RSI, sell overbought RSI.",
        params=(
            ParamSpec("period", "int", 14, 2, 100, "RSI lookback period (bars)"),
            ParamSpec("oversold", "float", 30, 1, 50, "RSI level to buy below"),
            ParamSpec("overbought", "float", 70, 50, 99, "RSI level to sell above"),
        ),
    ),
    "breakout_n": StrategySpec(
        name="breakout_n", fn=_breakout_n,
        label="N-Day Breakout",
        description="Donchian-style breakout of the previous N-day high/low; "
                    "shorts the downside breakdown when short selling is enabled.",
        params=(
            ParamSpec("entry_n", "int", 20, 2, 250, "Entry channel length (bars)"),
            ParamSpec("exit_n", "int", 10, 2, 250, "Exit channel length (bars)"),
        ),
    ),
    "momentum": StrategySpec(
        name="momentum", fn=_momentum,
        label="Momentum",
        description="Long when the M-day return exceeds the threshold; exits "
                    "when momentum turns; shorts the mirror signal when enabled.",
        params=(
            ParamSpec("lookback", "int", 20, 2, 250, "Return lookback (bars)"),
            ParamSpec("threshold", "float", 0.05, 0.0, 1.0,
                      "Entry return threshold (0.05 = 5%)"),
        ),
    ),
    "bollinger_revert": StrategySpec(
        name="bollinger_revert", fn=_bollinger_revert,
        label="Bollinger Mean Reversion",
        description="Buy below the lower Bollinger band, exit at the mean; "
                    "shorts above the upper band when short selling is enabled.",
        params=(
            ParamSpec("period", "int", 20, 5, 250, "Band lookback period (bars)"),
            ParamSpec("num_std", "float", 2.0, 0.5, 4.0, "Band width (std devs)"),
        ),
    ),
}
