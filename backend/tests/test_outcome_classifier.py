"""Unit tests for services.outcome_classifier.

Covers the 4-band classifier (`classify_outcome`), the multi-symbol
roll-up (`classify_discussion`), and the legacy-compat helpers
(`is_winning_verdict`, `is_losing_verdict`, `band_to_binary`).
"""
from __future__ import annotations

import pytest

from services.outcome_classifier import (
    band_to_binary,
    classify_discussion,
    classify_outcome,
    is_losing_verdict,
    is_winning_verdict,
)


def _closes_from_pct(d1_buy: float, pcts: list[float | None]) -> list[float | None]:
    """Build a closes list from percent changes vs d1_buy."""
    return [
        None if p is None else d1_buy * (1 + p / 100.0)
        for p in pcts
    ]


# ── classify_outcome — precedence ──────────────────────────────────


def test_big_loss_overrides_big_win_priority():
    """When trough ≤ -5% AND D5 ≥ +20%, 大敗優先 → big_loss."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [-6, +10, +15, +18, +22])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "big_loss"
    assert result.trough_pct == pytest.approx(-6.0)
    assert result.day5_pct == pytest.approx(22.0)


def test_spike_that_fades_by_d5_is_a_loss():
    """Peak +25% but D5 only +3% → loss.

    This is the case the pre-2026-07 peak-touch rule scored as a win:
    a position that spiked and gave it back. Nobody holding to the
    stated 5-day horizon realized +25%, so the band follows D5.
    """
    d1 = 100.0
    closes = _closes_from_pct(d1, [+25, +25, +25, +25, +3])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "loss"
    assert result.day5_pct == pytest.approx(3.0)
    # The peak is still reported — it just no longer decides the band.
    assert result.peak_pct == pytest.approx(25.0)


def test_big_win_d5_exact_threshold_inclusive():
    """D5 == +20% exactly → big_win (≥ is inclusive)."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+5, +10, +15, +18, +20])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "big_win"


def test_big_loss_at_exact_threshold_inclusive():
    """Any close at exactly -5% → big_loss (≤ is inclusive)."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+1, +1, -5, +1, +1])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "big_loss"


def test_win_at_exact_threshold_inclusive():
    """D5 at exactly +5% → win (≥ is inclusive)."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+1, +2, +3, +4, +5])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "win"
    assert result.day5_pct == pytest.approx(5.0)


def test_single_good_day_mid_window_does_not_make_a_win():
    """Single +6% day at D2 that fades to +1% by D5 → loss."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+1, +6, +2, +1, +1])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "loss"
    assert result.peak_pct == pytest.approx(6.0)
    assert result.day5_pct == pytest.approx(1.0)


def test_win_needs_the_gain_to_hold_to_d5():
    """Same +6% peak, but it holds → win."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+1, +6, +5, +6, +6])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "win"
    assert result.day5_pct == pytest.approx(6.0)


def test_loss_default_no_threshold_crossed():
    """No day ≥ +5%, no day ≤ -5% → loss."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+1, +2, -2, +3, +4])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "loss"
    assert result.peak_pct == pytest.approx(4.0)
    assert result.trough_pct == pytest.approx(-2.0)


# ── classify_outcome — partial windows ─────────────────────────────


def test_partial_window_is_undecidable_not_a_loss():
    """D2 hit +6% but D3-D5 are missing → no classification.

    Win/loss both hinge on the D5 close, so an open window has no
    honest answer. Returning `loss` would bias every partial window
    downward; callers all treat None as "skip this symbol".
    """
    d1 = 100.0
    closes = _closes_from_pct(d1, [+2, +6, None, None, None])
    assert classify_outcome(d1_buy=d1, closes=closes) is None


def test_partial_window_big_win_blocked_when_d5_missing():
    """Even with D2 == +25%, every positive band needs the D5 close."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+5, +25, None, None, None])
    assert classify_outcome(d1_buy=d1, closes=closes) is None


def test_partial_window_big_loss_still_detected():
    """Big_loss only needs ANY close ≤ -5% — partial window is fine."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [-7, None, None, None, None])
    result = classify_outcome(d1_buy=d1, closes=closes)
    assert result is not None
    assert result.band == "big_loss"


# ── classify_outcome — None / invalid inputs ───────────────────────


def test_returns_none_for_invalid_d1():
    assert classify_outcome(d1_buy=0.0, closes=[100.0]) is None
    assert classify_outcome(d1_buy=-5.0, closes=[100.0]) is None


def test_returns_none_when_all_closes_missing():
    d1 = 100.0
    result = classify_outcome(d1_buy=d1, closes=[None, None, None, None, None])
    assert result is None


def test_returns_none_when_closes_not_a_list():
    result = classify_outcome(d1_buy=100.0, closes=None)  # type: ignore[arg-type]
    assert result is None


# ── classify_outcome — custom thresholds ───────────────────────────


def test_custom_thresholds_respected():
    """Tighter thresholds change classification."""
    d1 = 100.0
    closes = _closes_from_pct(d1, [+3, +4, +3, +4, +4])
    # Default (+5% win bar): all peaks < 5% → loss
    assert classify_outcome(d1_buy=d1, closes=closes).band == "loss"
    # Tightened win bar to +3%: peak 4% ≥ 3% → win
    assert classify_outcome(d1_buy=d1, closes=closes, win_pct=3.0).band == "win"


# ── classify_discussion — multi-symbol roll-up ─────────────────────


def test_discussion_big_loss_overrides_big_win_across_symbols():
    """One symbol crashes -7%, another rips +25% by D5 → discussion big_loss."""
    a_closes = _closes_from_pct(100.0, [-7, -3, -2, -1, +1])
    b_closes = _closes_from_pct(100.0, [+5, +10, +15, +18, +22])
    result = classify_discussion(per_symbol={
        "A": (100.0, a_closes),
        "B": (100.0, b_closes),
    })
    assert result is not None
    assert result.band == "big_loss"
    assert result.winner_symbol == "A"


def test_discussion_big_win_when_no_big_loss():
    a_closes = _closes_from_pct(100.0, [+1, +2, -1, +1, +0])
    b_closes = _closes_from_pct(100.0, [+5, +10, +15, +18, +22])
    result = classify_discussion(per_symbol={
        "A": (100.0, a_closes),
        "B": (100.0, b_closes),
    })
    assert result is not None
    assert result.band == "big_win"
    assert result.winner_symbol == "B"


def test_discussion_win_picks_highest_d5():
    a_closes = _closes_from_pct(100.0, [+1, +6, +2, +6, +6])
    b_closes = _closes_from_pct(100.0, [+1, +8, +2, +7, +8])
    result = classify_discussion(per_symbol={
        "A": (100.0, a_closes),
        "B": (100.0, b_closes),
    })
    assert result is not None
    assert result.band == "win"
    assert result.winner_symbol == "B"


def test_discussion_loss_falls_through():
    a_closes = _closes_from_pct(100.0, [+1, +2, +3, +2, +1])
    b_closes = _closes_from_pct(100.0, [-1, -2, -3, -2, -1])
    result = classify_discussion(per_symbol={
        "A": (100.0, a_closes),
        "B": (100.0, b_closes),
    })
    assert result is not None
    assert result.band == "loss"


def test_discussion_returns_none_when_no_resolvable_symbol():
    result = classify_discussion(per_symbol={
        "A": (0.0, [100.0]),  # invalid d1
        "B": (100.0, [None, None]),  # no numeric closes
    })
    assert result is None


def test_discussion_empty_input_returns_none():
    assert classify_discussion(per_symbol={}) is None


# ── helpers ────────────────────────────────────────────────────────


def test_is_winning_verdict_legacy_compat():
    assert is_winning_verdict("win") is True
    assert is_winning_verdict("big_win") is True
    assert is_winning_verdict("loss") is False
    assert is_winning_verdict("big_loss") is False
    assert is_winning_verdict("unverifiable") is False
    assert is_winning_verdict(None) is False
    assert is_winning_verdict("") is False


def test_is_losing_verdict_legacy_compat():
    assert is_losing_verdict("loss") is True
    assert is_losing_verdict("big_loss") is True
    assert is_losing_verdict("win") is False
    assert is_losing_verdict("big_win") is False
    assert is_losing_verdict("unverifiable") is False
    assert is_losing_verdict(None) is False


def test_band_to_binary_winning():
    assert band_to_binary("win") == 1
    assert band_to_binary("big_win") == 1


def test_band_to_binary_losing_and_unknown():
    assert band_to_binary("loss") == 0
    assert band_to_binary("big_loss") == 0
    assert band_to_binary("unverifiable") == 0
    assert band_to_binary(None) == 0
    assert band_to_binary("garbage") == 0
