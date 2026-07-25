"""Tripwires for the macro-veto downgrade (spec Part 1 governance).

Pure functions — the monitor supplies data, these supply judgment.
Revert itself is deliberately human: automation that rewrites trading
rules unattended is a bigger risk than the alert-to-action delay.
"""
from __future__ import annotations

_DECIDED = ("win", "big_win", "loss", "big_loss")
_REVERT_INSTRUCTION = (
    "REVERT CONDITION MET for price_signal veto downgrade — run "
    "`python -m scripts.apply_veto_downgrade --revert` (one DB UPDATE; "
    "archived original text)."
)


def revert_trigger(verdicts_newest_first: list[str]) -> str | None:
    decided = [v for v in verdicts_newest_first if v in _DECIDED]
    streak = 0
    for v in decided:
        if v in ("loss", "big_loss"):
            streak += 1
            if streak >= 3:
                return f"3 consecutive decided losses. {_REVERT_INSTRUCTION}"
        else:
            break
    if sum(1 for v in decided[:10] if v == "big_loss") >= 2:
        return f"2 big_losses within the last 10 decided. {_REVERT_INSTRUCTION}"
    return None


def abstention_leakage(*, current_rate: float, baseline_rate: float) -> bool:
    """Prompt-scoped clause bleeding into other strategies shows up as
    their abstention rate collapsing. >20pp drop = investigate."""
    return (baseline_rate - current_rate) > 0.20
