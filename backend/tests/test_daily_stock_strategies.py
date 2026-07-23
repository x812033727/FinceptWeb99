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
    POOL_LIMIT,
    STRATEGIES,
    build_topic,
    candidate_batches,
    candidate_pool,
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


def test_chip_quality_ranks_stronger_on_both_halves_first():
    """Scores are mean percentile ranks across the pool, so the row that
    is better on more factors sorts first. The old absolute blend is
    gone: summing raw chip (~223,000, dominated by a share count) and
    quality (~70) never expressed the intended 50/50."""
    strong = _safe_row(
        symbol="2330",
        **{**_chip_fields(), "foreign_net_buy_5d": 9000, "return_5d": 0.09},
        **{**_quality_fields(), "roe": 30.0, "revenue_yoy": 25.0},
    )
    weak = _safe_row(
        symbol="1101",
        **{**_chip_fields(), "foreign_net_buy_5d": 1000, "return_5d": 0.01},
        **{**_quality_fields(), "roe": 10.0, "revenue_yoy": 5.0},
    )
    ranked = rank_candidates("chip_quality", [weak, strong])
    assert [r["symbol"] for r in ranked] == ["2330", "1101"]
    # Percentile means are bounded — no more five-figure scores.
    assert all(0.0 <= r["strategy_score"] <= 1.0 for r in ranked)


def test_single_candidate_scores_neutral():
    """One row has nothing to be ranked against, so every factor sits at
    the midpoint rather than inventing a spread."""
    row = _safe_row(**_chip_fields(), **_quality_fields())
    ranked = rank_candidates("chip_quality", [row])
    assert len(ranked) == 1
    assert ranked[0]["strategy_score"] == pytest.approx(0.5)


def test_an_extreme_factor_cannot_dominate_the_ranking():
    """The defect this replaces. 1808 carried revenue_yoy = +162,537%
    (a low-base artefact the earnings analyst rejected in session after
    session) and the raw sum ranked it first almost daily, because that
    one number was ~98% of its total score.

    Under percentile ranks the outlier tops exactly one of four factors
    and loses the other three, so it must not come first."""
    outlier = _safe_row(
        symbol="1808", revenue_yoy=162_537.0,
        return_5d=0.001, foreign_net_buy_5d=10, pe=29.0,
    )
    allrounder = _safe_row(
        symbol="2330", revenue_yoy=20.0,
        return_5d=0.08, foreign_net_buy_5d=900_000, pe=12.0,
    )
    middling = _safe_row(
        symbol="1101", revenue_yoy=15.0,
        return_5d=0.04, foreign_net_buy_5d=500_000, pe=18.0,
    )
    ranked = rank_candidates("general", [outlier, allrounder, middling])
    assert [r["symbol"] for r in ranked] == ["2330", "1101", "1808"]


def test_no_single_factor_exceeds_half_of_the_score():
    """Equal weighting over four factors caps any one factor's share at
    25% — the property whose absence let a share count contribute
    99.97% of a total."""
    rows = [
        _safe_row(symbol="2330", revenue_yoy=99_999.0, return_5d=0.01,
                  foreign_net_buy_5d=1, pe=25.0),
        _safe_row(symbol="1101", revenue_yoy=10.0, return_5d=0.05,
                  foreign_net_buy_5d=800_000, pe=15.0),
    ]
    ranked = rank_candidates("general", rows)
    # Four equally-weighted factors, each bounded on 0-1: the most any
    # one can contribute to the mean is 1/4.
    for row in ranked:
        assert row["strategy_score"] <= 1.0
        assert row["strategy_score"] >= 0.0


def test_tied_factor_values_share_a_rank():
    """Rows identical on a factor must not be ordered by it — otherwise
    a field that is missing pool-wide (and defaults everywhere) would
    silently drive the ranking."""
    rows = [
        _safe_row(symbol="2330", revenue_yoy=10.0, return_5d=0.05,
                  foreign_net_buy_5d=1000, pe=20.0),
        _safe_row(symbol="1101", revenue_yoy=10.0, return_5d=0.05,
                  foreign_net_buy_5d=1000, pe=20.0),
    ]
    ranked = rank_candidates("general", rows)
    assert ranked[0]["strategy_score"] == pytest.approx(ranked[1]["strategy_score"])
    # Stable tie-break by symbol, as before.
    assert [r["symbol"] for r in ranked] == ["1101", "2330"]


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
    assert ranked[0]["strategy_score"] == pytest.approx(0.5)  # sole row → neutral


def test_price_signal_oversold_track():
    row = _safe_row(symbol="1101", rsi=30.0, return_20d=-0.12, return_1d=0.01,
                    volume_ratio=1.2, foreign_net_buy_1d=500)
    ranked = rank_candidates("price_signal", [row])
    assert len(ranked) == 1
    assert ranked[0]["signal_type"] == "oversold"
    assert ranked[0]["strategy_score"] == pytest.approx(0.5)  # sole row → neutral


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


def test_candidate_pool_slims_rows_and_keeps_order():
    rows = [
        _safe_row(symbol=str(2000 + i),
                  **{**_chip_fields(), "foreign_net_buy_5d": 2000 + i},
                  **_quality_fields())
        for i in range(60)
    ]
    ranked = rank_candidates("chip_quality", rows)
    pool = candidate_pool(ranked)
    assert len(pool) == POOL_LIMIT == 50
    assert [p["symbol"] for p in pool] == [r["symbol"] for r in ranked[:50]]
    # Slim projection only — no raw indicator fields leak through.
    assert set(pool[0]) == {"symbol", "strategy_score"}


def test_candidate_pool_carries_signal_type_for_price_signal():
    breakout = _safe_row(close=105.0, prior_high_20d=100.0, volume_ratio=2.0,
                         rsi=65.0, return_20d=0.04)
    ranked = rank_candidates("price_signal", [breakout])
    pool = candidate_pool(ranked)
    assert pool == [
        {"symbol": "2330", "strategy_score": ranked[0]["strategy_score"],
         "signal_type": "breakout"},
    ]


def test_candidate_pool_empty_for_empty_ranking():
    assert candidate_pool([]) == []
