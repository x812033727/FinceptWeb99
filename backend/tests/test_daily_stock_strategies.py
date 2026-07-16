"""Tests for services.daily_stock_strategies — merged 3-strategy ranking.

chip_quality is the intersection of the old chip_momentum and
quality_growth gates with a 50/50 score blend; price_signal is the union
of the old breakout and oversold_reversal tracks with per-track scoring
and a signal_type tag. Old strategy keys are retired and must raise.
"""
from __future__ import annotations

import pytest

from services.daily_stock_strategies import (
    LABELS,
    STRATEGIES,
    build_topic,
    candidate_batches,
    rank_candidates,
)


def _safe_row(**overrides) -> dict:
    row = {
        "symbol": "2330",
        "is_etf": False,
        "is_warrant": False,
        "close": 100.0,
        "history_days": 60,
        "avg_volume_20d": 5_000_000,
        "prior_high_20d": 200.0,  # far above close: no accidental breakout
        "volume_ratio": 1.0,
        "rsi": 55.0,
    }
    row.update(overrides)
    return row


def _chip_fields() -> dict:
    return {"foreign_buy_days_5d": 4, "foreign_net_buy_5d": 2000, "return_5d": 0.03}


def _quality_fields() -> dict:
    return {
        "revenue_yoy": 15.0,
        "roe": 20.0,
        "operating_cash_flow": 5e8,
        "pe": 18.0,
        "ocf_positive_quarters": 4,
        "debt_ratio": 40.0,
    }


def test_strategy_registry():
    assert STRATEGIES == ("general", "chip_quality", "price_signal")
    assert LABELS["chip_quality"] == "籌碼品質"
    assert LABELS["price_signal"] == "量價訊號"


def test_chip_quality_requires_both_gates():
    chip_only = _safe_row(**_chip_fields())
    quality_only = _safe_row(**_quality_fields())
    assert rank_candidates("chip_quality", [chip_only]) == []
    assert rank_candidates("chip_quality", [quality_only]) == []


def test_chip_quality_intersection_scores_50_50_blend():
    row = _safe_row(**_chip_fields(), **_quality_fields())
    ranked = rank_candidates("chip_quality", [row])
    assert len(ranked) == 1
    # chip: 4*10 + 2000/1000 + 1.0*4 + 0.03*100 = 49
    # quality: 15 + 20 + 4*3 - 40/5 + (30-18) = 51
    assert ranked[0]["strategy_score"] == pytest.approx(0.5 * 49 + 0.5 * 51)


def test_chip_quality_empty_intersection_yields_no_batches():
    rows = [_safe_row(**_chip_fields()), _safe_row(symbol="1101", **_quality_fields())]
    ranked = rank_candidates("chip_quality", rows)
    assert ranked == []
    assert candidate_batches(ranked, run_count=3) == []


def test_price_signal_breakout_track():
    row = _safe_row(close=105.0, prior_high_20d=100.0, volume_ratio=2.0, rsi=65.0,
                    return_20d=0.04, foreign_net_buy_5d=2000)
    ranked = rank_candidates("price_signal", [row])
    assert len(ranked) == 1
    assert ranked[0]["signal_type"] == "breakout"
    # (105/100-1)*100 + 2*5 + 0.04*50 + 2000/2000 = 5 + 10 + 2 + 1 = 18
    assert ranked[0]["strategy_score"] == pytest.approx(18.0)


def test_price_signal_oversold_track():
    row = _safe_row(symbol="1101", rsi=30.0, return_20d=-0.12, return_1d=0.01,
                    volume_ratio=1.2, foreign_net_buy_1d=500)
    ranked = rank_candidates("price_signal", [row])
    assert len(ranked) == 1
    assert ranked[0]["signal_type"] == "oversold"
    # (40-30) + 0.01*100 + 1.2*3 + 500/1000 = 10 + 1 + 3.6 + 0.5 = 15.1
    assert ranked[0]["strategy_score"] == pytest.approx(15.1)


def test_price_signal_breakout_wins_when_both_tracks_match():
    row = _safe_row(close=105.0, prior_high_20d=100.0, volume_ratio=2.0,
                    rsi=35.0, return_20d=-0.10, return_1d=0.01)
    ranked = rank_candidates("price_signal", [row])
    assert ranked[0]["signal_type"] == "breakout"


def test_price_signal_mixed_tracks_sorted_by_score():
    breakout = _safe_row(close=105.0, prior_high_20d=100.0, volume_ratio=2.0,
                         rsi=65.0, return_20d=0.04, foreign_net_buy_5d=2000)
    oversold = _safe_row(symbol="1101", rsi=30.0, return_20d=-0.12, return_1d=0.01,
                         volume_ratio=1.2, foreign_net_buy_1d=500)
    ranked = rank_candidates("price_signal", [oversold, breakout])
    assert [r["signal_type"] for r in ranked] == ["breakout", "oversold"]
    scores = [r["strategy_score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_build_topic_price_signal_tags_signals():
    batch = [
        {"symbol": "2330", "signal_type": "breakout"},
        {"symbol": "1101", "signal_type": "oversold"},
    ]
    topic = build_topic("price_signal", batch, "unused")
    assert "量價訊號策略候選股" in topic
    assert "2330（突破）" in topic
    assert "1101（超跌）" in topic


def test_build_topic_chip_quality_plain_symbols():
    topic = build_topic("chip_quality", [{"symbol": "2330"}], "unused")
    assert "籌碼品質策略候選股：2330。" in topic


@pytest.mark.parametrize(
    "old_key", ["chip_momentum", "quality_growth", "breakout", "oversold_reversal"]
)
def test_old_strategy_keys_are_retired(old_key):
    with pytest.raises(ValueError, match="unknown strategy"):
        rank_candidates(old_key, [_safe_row()])
