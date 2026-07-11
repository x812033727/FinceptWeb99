"""Alert rule evaluators (PR-D1 規則引擎).

A registry of PURE functions, one per `condition_type`. Each takes
the alert row and an immutable `TickContext` (current quote fields +
any pre-computed daily thresholds) and returns a `FireResult` when
the rule matched, else None. No I/O in here — the service layer
fetches quotes / thresholds and handles cooldown, persistence and
notification, so adding a new rule type is: params model in
`schemas/alert.py` + one function here.

`foreign_net_buy_streak` is intentionally NOT registered as a tick
evaluator: it depends on daily institutional data, not the live
quote, and is evaluated by `tasks/alert_streaks_tw.py` after the TW
institutional ingest. It still shares the params model / API surface.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from models.alert import PriceAlert

# Threshold kinds pre-computed per (symbol, day) by the service layer.
THR_HIGH = "high"        # max(high) over lookback_days, excluding today
THR_LOW = "low"          # min(low) over lookback_days, excluding today
THR_AVG_VOL = "avg_vol"  # avg(volume) over lookback_days, excluding today

DEFAULT_LOOKBACK_DAYS = 20


@dataclass(frozen=True)
class TickContext:
    """One quote tick, as seen by the evaluators.

    `thresholds` maps (kind, lookback_days) → value, pre-fetched by
    the service for the rule types present on this symbol; a missing
    or None entry means "not enough daily data" and the evaluator
    abstains (returns None) rather than guessing.
    """
    price: float
    change_pct: float | None = None
    volume: float | None = None
    thresholds: dict[tuple[str, int], float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class FireResult:
    """A matched rule: zh-TW human message + structured payload for
    the alert_events row / WS push."""
    message: str
    payload: dict[str, Any]


Evaluator = Callable[[PriceAlert, TickContext], FireResult | None]


def _param(alert: PriceAlert, key: str, default: Any) -> Any:
    return (alert.params or {}).get(key, default)


def _lookback(alert: PriceAlert) -> int:
    return int(_param(alert, "lookback_days", DEFAULT_LOOKBACK_DAYS))


# ── evaluators ────────────────────────────────────────────────────


def eval_price_above(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    if alert.target_price is None or ctx.price < alert.target_price:
        return None
    return FireResult(
        message=(
            f"{alert.symbol} 價格高於目標 "
            f"{alert.target_price:g}(現價 {ctx.price:g})"
        ),
        payload={
            "condition": "above",
            "target_price": alert.target_price,
            "current_price": ctx.price,
        },
    )


def eval_price_below(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    if alert.target_price is None or ctx.price > alert.target_price:
        return None
    return FireResult(
        message=(
            f"{alert.symbol} 價格低於目標 "
            f"{alert.target_price:g}(現價 {ctx.price:g})"
        ),
        payload={
            "condition": "below",
            "target_price": alert.target_price,
            "current_price": ctx.price,
        },
    )


def eval_pct_change_above(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    pct = _param(alert, "pct", None)
    if pct is None or ctx.change_pct is None or ctx.change_pct < pct:
        return None
    return FireResult(
        message=f"{alert.symbol} 漲跌幅達 {pct:+g}%(現為 {ctx.change_pct:+.2f}%)",
        payload={"pct": pct, "change_pct": ctx.change_pct, "current_price": ctx.price},
    )


def eval_pct_change_below(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    pct = _param(alert, "pct", None)
    if pct is None or ctx.change_pct is None or ctx.change_pct > pct:
        return None
    return FireResult(
        message=f"{alert.symbol} 漲跌幅跌破 {pct:+g}%(現為 {ctx.change_pct:+.2f}%)",
        payload={"pct": pct, "change_pct": ctx.change_pct, "current_price": ctx.price},
    )


def eval_breakout_high(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    n = _lookback(alert)
    threshold = ctx.thresholds.get((THR_HIGH, n))
    if threshold is None or ctx.price <= threshold:
        return None
    return FireResult(
        message=(
            f"{alert.symbol} 突破 {n} 日高點 "
            f"{threshold:g}(現價 {ctx.price:g})"
        ),
        payload={
            "lookback_days": n,
            "threshold": threshold,
            "current_price": ctx.price,
        },
    )


def eval_breakout_low(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    n = _lookback(alert)
    threshold = ctx.thresholds.get((THR_LOW, n))
    if threshold is None or ctx.price >= threshold:
        return None
    return FireResult(
        message=(
            f"{alert.symbol} 跌破 {n} 日低點 "
            f"{threshold:g}(現價 {ctx.price:g})"
        ),
        payload={
            "lookback_days": n,
            "threshold": threshold,
            "current_price": ctx.price,
        },
    )


def eval_volume_surge(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    n = _lookback(alert)
    multiple = float(_param(alert, "multiple", 2.0))
    avg_vol = ctx.thresholds.get((THR_AVG_VOL, n))
    if not avg_vol or ctx.volume is None or ctx.volume < avg_vol * multiple:
        return None
    return FireResult(
        message=(
            f"{alert.symbol} 量能異常:成交量 {ctx.volume:,.0f} 達 "
            f"{n} 日均量 {avg_vol:,.0f} 的 {multiple:g} 倍以上"
        ),
        payload={
            "lookback_days": n,
            "multiple": multiple,
            "avg_volume": avg_vol,
            "current_volume": ctx.volume,
            "current_price": ctx.price,
        },
    )


# condition_type → evaluator. Tick-evaluated types only — daily-
# evaluated types (foreign_net_buy_streak) live in their own task.
TICK_EVALUATORS: dict[str, Evaluator] = {
    "price_above": eval_price_above,
    "price_below": eval_price_below,
    "pct_change_above": eval_pct_change_above,
    "pct_change_below": eval_pct_change_below,
    "breakout_high": eval_breakout_high,
    "breakout_low": eval_breakout_low,
    "volume_surge": eval_volume_surge,
}

# Daily-evaluated types, checked by scheduled tasks instead of ticks.
DAILY_CONDITION_TYPES = ("foreign_net_buy_streak",)


def threshold_needs(alerts: list[PriceAlert]) -> set[tuple[str, int]]:
    """Which (kind, lookback) thresholds must be resolved before the
    tick evaluators can run over this alert batch."""
    needs: set[tuple[str, int]] = set()
    for a in alerts:
        if a.condition_type == "breakout_high":
            needs.add((THR_HIGH, _lookback(a)))
        elif a.condition_type == "breakout_low":
            needs.add((THR_LOW, _lookback(a)))
        elif a.condition_type == "volume_surge":
            needs.add((THR_AVG_VOL, _lookback(a)))
    return needs
