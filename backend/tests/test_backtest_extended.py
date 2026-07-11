"""C2 backtest engine extension — deterministic synthetic-series tests.

Covers: stop-loss / take-profit / trailing-stop fill model, slippage +
commission arithmetic (exact expected equity), short-selling P&L sign
correctness, position sizing, each new built-in strategy's entry/exit
on a hand-built series, the strategy registry / param schemas, and a
byte-identical regression of the legacy default path.
"""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from analytics.backtest import (
    STRATEGIES,
    Order,
    list_strategies,
    run_backtest,
)

SYM = "TST"


def _df(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({SYM: [float(c) for c in closes]}, index=dates)


def _buy_at(bar_index: int, qty: float = 100.0):
    """Strategy stub: emit a single buy of `qty` shares at `bar_index`."""
    def strat(ctx, prices):
        if len(ctx.history) == bar_index + 1:
            return [Order(SYM, "buy", qty)]
        return []
    return strat


def _sell_at(bar_index: int, qty: float = 10.0):
    """Strategy stub: emit a single sell-to-open of `qty` shares."""
    def strat(ctx, prices):
        if len(ctx.history) == bar_index + 1:
            return [Order(SYM, "sell", qty)]
        return []
    return strat


def _script(orders_by_bar: dict[int, list[Order]]):
    def strat(ctx, prices):
        return orders_by_bar.get(len(ctx.history) - 1, [])
    return strat


# ── Legacy path regression ────────────────────────────────────────

class TestLegacyRegression:
    def test_default_config_byte_identical_to_pre_change_engine(self):
        """A run with only legacy arguments must reproduce the exact
        pre-C2 engine output (hash captured from the engine before this
        change on the same seeded series)."""
        rng = np.random.default_rng(7)
        dates = pd.date_range("2021-01-04", periods=180, freq="B").strftime("%Y-%m-%d")
        prices = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, 180)))
        df = pd.DataFrame({"XYZ": prices}, index=dates)

        res = run_backtest(df, "sma_crossover", {"symbols": ["XYZ"], "fast": 5, "slow": 20})

        digest = hashlib.sha256(
            json.dumps(res, sort_keys=True).encode()
        ).hexdigest()
        assert digest == (
            "7d7dee23719e3c586e044353d311a377bf9759784c57895f048e8c143571fec1"
        )
        # Readable spot-checks so a failure isn't just an opaque hash.
        assert res["metrics"] == {
            "total_return_pct": -11.6,
            "annualised_return_pct": -15.85,
            "annualised_volatility": 9.36,
            "sharpe_ratio": -1.6939,
            "max_drawdown_pct": -14.31,
            "win_rate": 0.75,
            "total_trades": 8,
            "final_value": 88400.49,
        }
        # Legacy trades carry exactly the legacy keys — no enrichment.
        assert set(res["trades"][0]) == {"date", "symbol", "side", "quantity", "price"}
        assert "total_commission" not in res["metrics"]

    def test_legacy_keyword_defaults_equal_explicit_offs(self):
        rng = np.random.default_rng(11)
        dates = pd.date_range("2022-01-03", periods=120, freq="B").strftime("%Y-%m-%d")
        prices = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 120)))
        df = pd.DataFrame({"XYZ": prices}, index=dates)
        a = run_backtest(df, "sma_crossover", {"symbols": ["XYZ"], "fast": 5, "slow": 20})
        b = run_backtest(
            df, "sma_crossover", {"symbols": ["XYZ"], "fast": 5, "slow": 20},
            stop_loss_pct=None, take_profit_pct=None, trailing_stop_pct=None,
            position_size_pct=None, slippage_bps=0.0, commission_bps=0.0,
            allow_short=False,
        )
        assert a == b


# ── Risk controls ─────────────────────────────────────────────────

class TestStopLoss:
    def test_gap_through_stop_fills_at_close(self):
        # Entry 100 @ bar5, stop 10% → level 90; bar10 gaps to 89.
        closes = [100] * 10 + [89] + [89] * 9
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           stop_loss_pct=0.10)
        assert res["status"] == "completed"
        trades = res["trades"]
        assert len(trades) == 2
        exit_t = trades[1]
        assert exit_t["side"] == "sell"
        assert exit_t["date"] == "2024-01-11"        # bar index 10
        assert exit_t["price"] == pytest.approx(89)  # worse-of(level, close)
        assert exit_t["exit_reason"] == "stop"
        # Cash: 100000 - 100*100 + 100*89 = 98900
        assert res["metrics"]["final_value"] == pytest.approx(98900.0)

    def test_exact_level_touch_fills_at_level(self):
        closes = [100] * 10 + [90] + [90] * 9
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           stop_loss_pct=0.10)
        exit_t = res["trades"][1]
        assert exit_t["price"] == pytest.approx(90.0)
        assert exit_t["exit_reason"] == "stop"

    def test_no_trigger_above_level(self):
        closes = [100] * 10 + [91] * 10   # never reaches 90
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           stop_loss_pct=0.10)
        assert len(res["trades"]) == 1    # entry only, never exited

    def test_short_stop_mirror(self):
        # Short 100 sh @100 bar5, stop 5% → level 105; bar8 gaps to 108.
        closes = [100] * 8 + [108] + [108] * 11
        res = run_backtest(_df(closes), _sell_at(5, qty=100), {},
                           commission=0.0, stop_loss_pct=0.05,
                           allow_short=True)
        exit_t = res["trades"][1]
        assert exit_t["side"] == "buy"
        assert exit_t["price"] == pytest.approx(108)  # worse-of(105, 108)
        assert exit_t["exit_reason"] == "stop"
        # Loss = (100 - 108) * 100 = -800
        assert res["metrics"]["final_value"] == pytest.approx(99200.0)


class TestTakeProfit:
    def test_fills_at_level_not_the_better_close(self):
        # Entry 100 @ bar5, TP 10% → level 110; bar10 closes at 115.
        closes = [100] * 10 + [115] + [115] * 9
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           take_profit_pct=0.10)
        exit_t = res["trades"][1]
        assert exit_t["date"] == "2024-01-11"
        assert exit_t["price"] == pytest.approx(110.0)   # limit fill at level
        assert exit_t["exit_reason"] == "take_profit"
        assert res["metrics"]["final_value"] == pytest.approx(101000.0)

    def test_stop_beats_take_profit_same_bar(self):
        # Degenerate whipsaw: tp 1%, stop 1%, bar drops 5% → stop wins.
        closes = [100] * 10 + [95] + [95] * 9
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           stop_loss_pct=0.01, take_profit_pct=0.01)
        assert res["trades"][1]["exit_reason"] == "stop"


class TestTrailingStop:
    def test_ratchet_and_trigger(self):
        # Entry @100 (bar5); rises 110 → 120 (peak); dips to 112 (held,
        # level = 120*0.9 = 108); then 107 ≤ 108 triggers.
        closes = [100] * 6 + [110, 120, 112, 107] + [107] * 10
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           trailing_stop_pct=0.10)
        trades = res["trades"]
        assert len(trades) == 2
        exit_t = trades[1]
        assert exit_t["date"] == "2024-01-10"            # bar index 9
        assert exit_t["price"] == pytest.approx(107.0)   # worse-of(108, 107)
        assert exit_t["exit_reason"] == "trailing"
        # Cash: 100000 - 10000 + 100*107 = 100700
        assert res["metrics"]["final_value"] == pytest.approx(100700.0)

    def test_dip_above_ratcheted_level_holds(self):
        closes = [100] * 6 + [110, 120, 112, 109] + [120] * 10
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           trailing_stop_pct=0.10)
        assert len(res["trades"]) == 1   # 109 > 108 → never exited


class TestCosts:
    def test_slippage_and_commission_exact_equity(self):
        closes = [100] * 20
        strat = _script({5: [Order(SYM, "buy", 100)],
                         10: [Order(SYM, "sell", 100)]})
        res = run_backtest(_df(closes), strat, {}, commission=0.0,
                           slippage_bps=10, commission_bps=20)
        buy, sell = res["trades"]
        # Buy fills 0.1% worse: 100.1; fee = 10010 * 0.002 = 20.02
        assert buy["price"] == pytest.approx(100.1)
        assert buy["commission"] == pytest.approx(20.02)
        assert buy["slippage"] == pytest.approx(10.0)
        # Sell fills 0.1% worse: 99.9; fee = 9990 * 0.002 = 19.98
        assert sell["price"] == pytest.approx(99.9)
        assert sell["commission"] == pytest.approx(19.98)
        assert sell["slippage"] == pytest.approx(10.0)
        # Cash: 100000 - 10010 - 20.02 + 9990 - 19.98 = 99940.00
        assert res["metrics"]["final_value"] == pytest.approx(99940.0)
        assert res["metrics"]["total_commission"] == pytest.approx(40.0)
        assert res["metrics"]["total_slippage"] == pytest.approx(20.0)

    def test_costs_also_apply_to_risk_exits(self):
        closes = [100] * 10 + [89] + [89] * 9
        res = run_backtest(_df(closes), _buy_at(5), {}, commission=0.0,
                           stop_loss_pct=0.10, slippage_bps=100)
        exit_t = res["trades"][1]
        # Stop fill at close 89, then 1% sell slippage → 88.11
        assert exit_t["price"] == pytest.approx(89 * 0.99)
        assert exit_t["exit_reason"] == "stop"


class TestShortSelling:
    def test_short_profit_sign(self):
        # Short 10 @100 (bar5), cover @90 (bar12): P&L = +100.
        closes = [100] * 10 + [90] * 10
        strat = _script({5: [Order(SYM, "sell", 10)],
                         12: [Order(SYM, "buy", 10)]})
        res = run_backtest(_df(closes), strat, {}, commission=0.0,
                           allow_short=True)
        assert res["metrics"]["final_value"] == pytest.approx(100100.0)
        cover = res["trades"][1]
        assert cover["side"] == "buy"
        assert cover["exit_reason"] == "signal"

    def test_short_loss_sign(self):
        closes = [100] * 10 + [110] * 10
        strat = _script({5: [Order(SYM, "sell", 10)],
                         12: [Order(SYM, "buy", 10)]})
        res = run_backtest(_df(closes), strat, {}, commission=0.0,
                           allow_short=True)
        assert res["metrics"]["final_value"] == pytest.approx(99900.0)

    def test_short_equity_marks_liability(self):
        # While short 10 @100 with price at 90, equity = cash + qty·px
        # = 101000 - 900 = 100100 (unrealised +100).
        closes = [100] * 10 + [90] * 10
        res = run_backtest(_df(closes), _sell_at(5, qty=10), {},
                           commission=0.0, allow_short=True)
        eq = {e["date"]: e["value"] for e in res["equity_curve"]}
        assert eq["2024-01-11"] == pytest.approx(100100.0)

    def test_short_rejected_without_flag(self):
        closes = [100] * 20
        res = run_backtest(_df(closes), _sell_at(5, qty=10), {},
                           commission=0.0)
        assert res["trades"] == []   # naked sell dropped (legacy rule)


class TestPositionSizing:
    def test_entry_resized_to_equity_fraction(self):
        closes = [100] * 20
        res = run_backtest(_df(closes), _buy_at(5, qty=1), {},
                           commission=0.0, position_size_pct=0.5)
        assert res["trades"][0]["quantity"] == pytest.approx(500.0)  # 50k / 100
        assert res["metrics"]["final_value"] == pytest.approx(100000.0)

    def test_long_entry_capped_by_cash(self):
        closes = [100] * 20
        # 100% of equity with fees would overdraw — cap keeps it affordable.
        res = run_backtest(_df(closes), _buy_at(5, qty=1), {},
                           commission=0.001, position_size_pct=1.0)
        t = res["trades"][0]
        assert t["quantity"] * t["price"] * 1.001 <= 100000.0 + 1e-6


# ── New built-in strategies ───────────────────────────────────────

class TestBreakoutN:
    def test_long_entry_and_exit(self):
        # Prev-5-high = 100 → close 106 at bar6 breaks out; close 105 at
        # bar9 undercuts prev-3-low (106) → exit.
        closes = [100] * 6 + [106, 107, 108, 105] + [105] * 10
        res = run_backtest(_df(closes), "breakout_n",
                           {"symbols": [SYM], "entry_n": 5, "exit_n": 3},
                           commission=0.0)
        trades = res["trades"]
        assert [t["side"] for t in trades] == ["buy", "sell"]
        assert trades[0]["date"] == "2024-01-07"   # bar 6
        assert trades[0]["price"] == pytest.approx(106.0)
        assert trades[1]["date"] == "2024-01-10"   # bar 9
        assert trades[1]["price"] == pytest.approx(105.0)

    def test_short_breakdown_when_allowed(self):
        closes = [100] * 6 + [94] + [94] * 13
        res = run_backtest(_df(closes), "breakout_n",
                           {"symbols": [SYM], "entry_n": 5, "exit_n": 3},
                           commission=0.0, allow_short=True)
        assert res["trades"][0]["side"] == "sell"
        assert res["trades"][0]["date"] == "2024-01-07"

    def test_no_short_without_flag(self):
        closes = [100] * 6 + [94] + [94] * 13
        res = run_backtest(_df(closes), "breakout_n",
                           {"symbols": [SYM], "entry_n": 5, "exit_n": 3},
                           commission=0.0)
        assert res["trades"] == []


class TestMomentum:
    def test_entry_on_threshold_cross_and_exit_on_reversal(self):
        # 5-day return first exceeds 5% at bar8 (106/100); momentum
        # first turns negative at bar13 (105/106). Bar7 is kept at
        # 104.9 (4.9%) so no float-rounding threshold graze.
        closes = [100] * 5 + [101, 103, 104.9, 106] + [106] * 4 + [105] * 7
        res = run_backtest(_df(closes), "momentum",
                           {"symbols": [SYM], "lookback": 5, "threshold": 0.05},
                           commission=0.0)
        trades = res["trades"]
        assert [t["side"] for t in trades] == ["buy", "sell"]
        assert trades[0]["date"] == "2024-01-09"   # bar 8
        assert trades[0]["price"] == pytest.approx(106.0)
        assert trades[1]["date"] == "2024-01-14"   # bar 13
        assert trades[1]["price"] == pytest.approx(105.0)

    def test_short_on_negative_momentum(self):
        closes = [100] * 5 + [99, 97, 95.1, 93] + [93] * 11
        res = run_backtest(_df(closes), "momentum",
                           {"symbols": [SYM], "lookback": 5, "threshold": 0.05},
                           commission=0.0, allow_short=True)
        assert res["trades"][0]["side"] == "sell"
        assert res["trades"][0]["date"] == "2024-01-09"  # 93/100-1 = -7%


class TestBollingerRevert:
    def test_buy_below_lower_band_exit_at_mean(self):
        closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 90, 100] + [100] * 8
        res = run_backtest(_df(closes), "bollinger_revert",
                           {"symbols": [SYM], "period": 10, "num_std": 2.0},
                           commission=0.0)
        trades = res["trades"]
        assert [t["side"] for t in trades] == ["buy", "sell"]
        assert trades[0]["date"] == "2024-01-11"   # bar 10, close 90
        assert trades[0]["price"] == pytest.approx(90.0)
        assert trades[1]["date"] == "2024-01-12"   # reverts to ≥ mean
        assert trades[1]["price"] == pytest.approx(100.0)

    def test_flat_series_never_trades(self):
        closes = [100] * 30   # σ = 0 → band guard blocks entries
        res = run_backtest(_df(closes), "bollinger_revert",
                           {"symbols": [SYM], "period": 10}, commission=0.0)
        assert res["trades"] == []

    def test_short_above_upper_band_when_allowed(self):
        closes = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 112, 100] + [100] * 8
        res = run_backtest(_df(closes), "bollinger_revert",
                           {"symbols": [SYM], "period": 10, "num_std": 2.0},
                           commission=0.0, allow_short=True)
        assert res["trades"][0]["side"] == "sell"
        assert res["trades"][0]["date"] == "2024-01-11"


# ── Registry / param schemas ──────────────────────────────────────

class TestStrategyRegistry:
    def test_all_five_strategies_registered(self):
        assert set(STRATEGIES) == {
            "sma_crossover", "rsi_mean_reversion",
            "breakout_n", "momentum", "bollinger_revert",
        }

    def test_list_strategies_schema_shape(self):
        specs = {s["name"]: s for s in list_strategies()}
        assert len(specs) == 5
        for spec in specs.values():
            assert spec["label"]
            assert isinstance(spec["params"], list)
            for p in spec["params"]:
                assert set(p) == {"name", "type", "default", "min", "max", "description"}
                assert p["type"] in ("int", "float")
        assert [p["name"] for p in specs["breakout_n"]["params"]] == ["entry_n", "exit_n"]
        assert [p["name"] for p in specs["momentum"]["params"]] == ["lookback", "threshold"]
        assert [p["name"] for p in specs["bollinger_revert"]["params"]] == ["period", "num_std"]

    def test_unknown_strategy_falls_back_to_sma(self):
        # Legacy engine behaviour preserved: unknown names run sma_crossover.
        rng = np.random.default_rng(3)
        dates = pd.date_range("2022-01-03", periods=60, freq="B").strftime("%Y-%m-%d")
        prices = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 60)))
        df = pd.DataFrame({"XYZ": prices}, index=dates)
        a = run_backtest(df, "definitely_not_a_strategy", {"symbols": ["XYZ"]})
        b = run_backtest(df, "sma_crossover", {"symbols": ["XYZ"]})
        assert a == b
