"""Pure unit tests for the alert rule evaluator registry (PR-D1).

Evaluators are pure functions over (PriceAlert, TickContext) — no DB,
no Redis — so every boundary case is a plain construct-and-assert.
Cooldown/repeat semantics and threshold resolution are integration-
tested in test_alert_service.py.
"""
from models.alert import PriceAlert
from schemas.alert import PARAMS_MODELS
from services.alert_rules import (
    DAILY_CONDITION_TYPES,
    THR_AVG_VOL,
    THR_HIGH,
    THR_LOW,
    TICK_EVALUATORS,
    TickContext,
    threshold_needs,
)


def _alert(condition_type: str, *, target_price=None, params=None) -> PriceAlert:
    return PriceAlert(
        symbol="TST", market="US",
        condition_type=condition_type,
        target_price=target_price, params=params,
    )


def _ctx(price=100.0, change_pct=None, volume=None, thresholds=None) -> TickContext:
    return TickContext(
        price=price, change_pct=change_pct, volume=volume,
        thresholds=thresholds or {},
    )


# ── registry consistency ─────────────────────────────────────────

def test_registry_covers_every_condition_type():
    """Every API-accepted condition_type has exactly one evaluator:
    either per-tick or the daily task."""
    assert set(PARAMS_MODELS) == set(TICK_EVALUATORS) | set(DAILY_CONDITION_TYPES)
    assert not set(TICK_EVALUATORS) & set(DAILY_CONDITION_TYPES)


# ── price_above / price_below ────────────────────────────────────

def test_price_above_fires_at_and_above_target():
    ev = TICK_EVALUATORS["price_above"]
    alert = _alert("price_above", target_price=200.0)
    assert ev(alert, _ctx(price=200.0)) is not None  # >= boundary
    assert ev(alert, _ctx(price=200.01)) is not None
    assert ev(alert, _ctx(price=199.99)) is None


def test_price_above_message_and_payload():
    res = TICK_EVALUATORS["price_above"](
        _alert("price_above", target_price=200.0), _ctx(price=205.0),
    )
    assert "高於" in res.message and "TST" in res.message
    assert res.payload == {
        "condition": "above", "target_price": 200.0, "current_price": 205.0,
    }


def test_price_below_fires_at_and_below_target():
    ev = TICK_EVALUATORS["price_below"]
    alert = _alert("price_below", target_price=150.0)
    assert ev(alert, _ctx(price=150.0)) is not None  # <= boundary
    assert ev(alert, _ctx(price=149.5)) is not None
    assert ev(alert, _ctx(price=150.01)) is None


def test_price_rules_abstain_without_target():
    assert TICK_EVALUATORS["price_above"](_alert("price_above"), _ctx()) is None
    assert TICK_EVALUATORS["price_below"](_alert("price_below"), _ctx()) is None


# ── pct_change_above / pct_change_below ──────────────────────────

def test_pct_change_above_boundary():
    ev = TICK_EVALUATORS["pct_change_above"]
    alert = _alert("pct_change_above", params={"pct": 5.0})
    assert ev(alert, _ctx(change_pct=5.0)) is not None   # >= boundary
    assert ev(alert, _ctx(change_pct=6.2)) is not None
    assert ev(alert, _ctx(change_pct=4.99)) is None


def test_pct_change_below_boundary_negative_threshold():
    ev = TICK_EVALUATORS["pct_change_below"]
    alert = _alert("pct_change_below", params={"pct": -3.0})
    assert ev(alert, _ctx(change_pct=-3.0)) is not None  # <= boundary
    assert ev(alert, _ctx(change_pct=-7.5)) is not None
    assert ev(alert, _ctx(change_pct=-2.9)) is None


def test_pct_change_abstains_without_quote_change_pct():
    alert = _alert("pct_change_above", params={"pct": 1.0})
    assert TICK_EVALUATORS["pct_change_above"](alert, _ctx(change_pct=None)) is None


def test_pct_change_payload():
    res = TICK_EVALUATORS["pct_change_above"](
        _alert("pct_change_above", params={"pct": 5.0}),
        _ctx(price=110.0, change_pct=6.25),
    )
    assert res.payload["pct"] == 5.0
    assert res.payload["change_pct"] == 6.25
    assert res.payload["current_price"] == 110.0


# ── breakout_high / breakout_low ─────────────────────────────────

def test_breakout_high_strictly_above_threshold():
    ev = TICK_EVALUATORS["breakout_high"]
    alert = _alert("breakout_high", params={"lookback_days": 20})
    thr = {(THR_HIGH, 20): 105.0}
    assert ev(alert, _ctx(price=105.01, thresholds=thr)) is not None
    assert ev(alert, _ctx(price=105.0, thresholds=thr)) is None  # touch ≠ breakout
    assert ev(alert, _ctx(price=104.0, thresholds=thr)) is None


def test_breakout_low_strictly_below_threshold():
    ev = TICK_EVALUATORS["breakout_low"]
    alert = _alert("breakout_low", params={"lookback_days": 10})
    thr = {(THR_LOW, 10): 95.0}
    assert ev(alert, _ctx(price=94.99, thresholds=thr)) is not None
    assert ev(alert, _ctx(price=95.0, thresholds=thr)) is None
    assert ev(alert, _ctx(price=96.0, thresholds=thr)) is None


def test_breakout_abstains_without_threshold():
    """No daily bars → threshold None → never fires."""
    alert = _alert("breakout_high", params={"lookback_days": 20})
    assert TICK_EVALUATORS["breakout_high"](alert, _ctx(price=999.0)) is None
    alert2 = _alert("breakout_high", params={"lookback_days": 20})
    ctx = _ctx(price=999.0, thresholds={(THR_HIGH, 20): None})
    assert TICK_EVALUATORS["breakout_high"](alert2, ctx) is None


def test_breakout_default_lookback_is_20():
    alert = _alert("breakout_high", params={})
    ctx = _ctx(price=106.0, thresholds={(THR_HIGH, 20): 105.0})
    assert TICK_EVALUATORS["breakout_high"](alert, ctx) is not None


# ── volume_surge ─────────────────────────────────────────────────

def test_volume_surge_boundary():
    ev = TICK_EVALUATORS["volume_surge"]
    alert = _alert("volume_surge", params={"multiple": 2.0, "lookback_days": 20})
    thr = {(THR_AVG_VOL, 20): 1000.0}
    assert ev(alert, _ctx(volume=2000.0, thresholds=thr)) is not None  # >= boundary
    assert ev(alert, _ctx(volume=2500.0, thresholds=thr)) is not None
    assert ev(alert, _ctx(volume=1999.0, thresholds=thr)) is None


def test_volume_surge_abstains_without_data():
    ev = TICK_EVALUATORS["volume_surge"]
    alert = _alert("volume_surge", params={"multiple": 2.0, "lookback_days": 20})
    # no avg volume threshold
    assert ev(alert, _ctx(volume=99999.0)) is None
    # no tick volume
    thr = {(THR_AVG_VOL, 20): 1000.0}
    alert2 = _alert("volume_surge", params={"multiple": 2.0, "lookback_days": 20})
    assert ev(alert2, _ctx(volume=None, thresholds=thr)) is None
    # zero avg volume (fresh listing) must not fire on any volume
    thr0 = {(THR_AVG_VOL, 20): 0.0}
    assert ev(alert2, _ctx(volume=1.0, thresholds=thr0)) is None


# ── threshold_needs ──────────────────────────────────────────────

def test_threshold_needs_collects_per_type_lookbacks():
    alerts = [
        _alert("breakout_high", params={"lookback_days": 20}),
        _alert("breakout_high", params={"lookback_days": 60}),
        _alert("breakout_low", params={"lookback_days": 10}),
        _alert("volume_surge", params={"multiple": 3.0, "lookback_days": 20}),
        _alert("price_above", target_price=1.0),
        _alert("pct_change_above", params={"pct": 5.0}),
    ]
    assert threshold_needs(alerts) == {
        (THR_HIGH, 20), (THR_HIGH, 60), (THR_LOW, 10), (THR_AVG_VOL, 20),
    }


def test_threshold_needs_empty_for_price_rules():
    assert threshold_needs([_alert("price_above", target_price=1.0)]) == set()
