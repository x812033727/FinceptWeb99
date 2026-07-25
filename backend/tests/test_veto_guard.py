"""Post-adoption tripwires for the macro-veto downgrade (spec Part 1).

Revert stays a HUMAN action — the guard's job is to say, loudly and
with instructions, when the pre-registered revert condition is met."""
from __future__ import annotations

from services.veto_guard import abstention_leakage, revert_trigger


def test_three_consecutive_losses_fires():
    msg = revert_trigger(["loss", "big_loss", "loss", "win", "win"])
    assert msg is not None and "revert" in msg.lower()


def test_wins_break_the_streak():
    assert revert_trigger(["loss", "win", "loss", "loss"]) is None


def test_two_big_losses_in_rolling_ten_fires():
    verdicts = ["big_loss"] + ["win"] * 5 + ["big_loss"] + ["win"] * 3
    assert revert_trigger(verdicts) is not None


def test_two_big_losses_spread_beyond_ten_is_quiet():
    verdicts = ["big_loss"] + ["win"] * 10 + ["big_loss"]
    assert revert_trigger(verdicts) is None


def test_abstains_are_ignored_by_the_streak():
    assert revert_trigger(["loss", "abstain", "loss", "abstain", "loss"]) is not None


def test_leakage_threshold():
    assert abstention_leakage(current_rate=0.30, baseline_rate=0.55) is True
    assert abstention_leakage(current_rate=0.40, baseline_rate=0.55) is False
