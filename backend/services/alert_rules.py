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
from datetime import UTC, datetime
import math
from typing import Any

from models.alert import PriceAlert
from schemas.alert import parse_trend_time

# Threshold kinds pre-computed per (symbol, day) by the service layer.
THR_HIGH = "high"        # max(high) over lookback_days, excluding today
THR_LOW = "low"          # min(low) over lookback_days, excluding today
THR_AVG_VOL = "avg_vol"  # avg(volume) over lookback_days, excluding today
THR_CLOSES = "closes"     # ordered prior closes used by live indicators

DEFAULT_LOOKBACK_DAYS = 20
RSI_HISTORY_BARS = 252
TREND_CONDITION_TYPES = ("trend_cross_above", "trend_cross_below")
RSI_CONDITION_TYPES = ("rsi_cross_above", "rsi_cross_below")
STATEFUL_CONDITION_TYPES = TREND_CONDITION_TYPES + RSI_CONDITION_TYPES


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
    thresholds: dict[tuple[str, int], Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


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


def trend_line_projection(alert: PriceAlert, observed_at: datetime) -> float | None:
    """Calendar-time linear interpolation/extrapolation for a trend rule.

    Returns None for malformed legacy/manual rows or a projection at/below
    zero; evaluators abstain instead of producing an impossible target.
    """
    params = alert.params or {}
    try:
        start_at = parse_trend_time(str(params["start_time"]))
        end_at = parse_trend_time(str(params["end_time"]))
        start_price = float(params["start_price"])
        end_price = float(params["end_price"])
        at = observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=UTC)
        at = at.astimezone(UTC)
        span = (end_at - start_at).total_seconds()
        if span == 0:
            return None
        projected = start_price + (end_price - start_price) * (
            (at - start_at).total_seconds() / span
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(projected) or projected <= 0:
        return None
    return projected


def trend_relation(alert: PriceAlert, ctx: TickContext) -> str | None:
    """Current quote's side of the projected line, with a tiny FP deadband."""
    target = trend_line_projection(alert, ctx.observed_at)
    if target is None:
        return None
    epsilon = max(abs(target) * 1e-9, 1e-9)
    if ctx.price > target + epsilon:
        return "above"
    if ctx.price < target - epsilon:
        return "below"
    return "on"


def _eval_trend_cross(
    alert: PriceAlert, ctx: TickContext, direction: str,
) -> FireResult | None:
    relation = trend_relation(alert, ctx)
    matches = relation in ({"above", "on"} if direction == "above" else {"below", "on"})
    if relation is None or not matches:
        return None
    target = trend_line_projection(alert, ctx.observed_at)
    assert target is not None
    verb = "向上突破" if direction == "above" else "向下跌破"
    return FireResult(
        message=(
            f"{alert.symbol} {verb}趨勢線 {target:g}"
            f"(現價 {ctx.price:g})"
        ),
        payload={
            "condition": direction,
            "projected_price": target,
            "current_price": ctx.price,
            "observed_at": ctx.observed_at.isoformat(),
        },
    )


def eval_trend_cross_above(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    return _eval_trend_cross(alert, ctx, "above")


def eval_trend_cross_below(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    return _eval_trend_cross(alert, ctx, "below")


def rsi_value(alert: PriceAlert, ctx: TickContext) -> float | None:
    """RSI from `period` archived closes plus the current live quote.

    The archived list is strictly before today's live bar. The first window
    seeds average gain/loss, then every later close (including the live quote)
    applies Wilder smoothing so values align with the chart indicator.
    """
    period = int(_param(alert, "period", 14))
    closes = ctx.thresholds.get((THR_CLOSES, RSI_HISTORY_BARS))
    if not isinstance(closes, (list, tuple)) or len(closes) < period:
        return None
    try:
        values = [float(value) for value in closes] + [float(ctx.price)]
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(value) or value <= 0 for value in values):
        return None
    initial_changes = [values[index] - values[index - 1] for index in range(1, period + 1)]
    avg_gain = sum(max(change, 0.0) for change in initial_changes) / period
    avg_loss = sum(max(-change, 0.0) for change in initial_changes) / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def rsi_relation(alert: PriceAlert, ctx: TickContext) -> str | None:
    value = rsi_value(alert, ctx)
    if value is None:
        return None
    default_level = 30 if alert.condition_type == "rsi_cross_below" else 70
    level = float(_param(alert, "level", default_level))
    if value > level + 1e-9:
        return "above"
    if value < level - 1e-9:
        return "below"
    return "on"


def _eval_rsi_cross(
    alert: PriceAlert, ctx: TickContext, direction: str,
) -> FireResult | None:
    relation = rsi_relation(alert, ctx)
    matches = relation in ({"above", "on"} if direction == "above" else {"below", "on"})
    if relation is None or not matches:
        return None
    value = rsi_value(alert, ctx)
    assert value is not None
    period = int(_param(alert, "period", 14))
    level = float(_param(alert, "level", 70 if direction == "above" else 30))
    verb = "向上穿越" if direction == "above" else "向下穿越"
    return FireResult(
        message=f"{alert.symbol} RSI({period}) {verb} {level:g}(現為 {value:.2f})",
        payload={
            "condition": direction,
            "period": period,
            "level": level,
            "rsi": value,
            "current_price": ctx.price,
        },
    )


def eval_rsi_cross_above(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    return _eval_rsi_cross(alert, ctx, "above")


def eval_rsi_cross_below(alert: PriceAlert, ctx: TickContext) -> FireResult | None:
    return _eval_rsi_cross(alert, ctx, "below")


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
    "trend_cross_above": eval_trend_cross_above,
    "trend_cross_below": eval_trend_cross_below,
    "rsi_cross_above": eval_rsi_cross_above,
    "rsi_cross_below": eval_rsi_cross_below,
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
        elif a.condition_type in RSI_CONDITION_TYPES:
            needs.add((THR_CLOSES, RSI_HISTORY_BARS))
    return needs
