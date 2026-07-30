"""Observed consensus — measured from the transcript, not self-reported.

`conclusion["consensus_score"]` is a number the synthesizer LLM writes
about itself. Nothing ever checked it against what the personas
actually said, and in production the two are decoupled: a chip_quality
run on 2026-07-21 reported `consensus_score = 0.88` over a transcript of
0 `agree` and 36 `dissent` turns.

This module derives a second number from the turns themselves so the
gap between claim and transcript becomes a value you can query, chart
and calibrate against. It deliberately does NOT replace the
self-reported score: the disagreement is the finding.

Pure functions — no DB, no I/O — so the scoring rule can be tested
directly on stance sequences.
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol


class _Turn(Protocol):
    """Structural view of `models.discussion_turn.DiscussionTurn`."""
    round: int
    stance: str


# How much each stance counts toward "the room agreed".
#
#   agree      — endorses the standing view outright.
#   supplement — adds without opposing. Counted as partial agreement:
#                a supplementing analyst is not a dissenting one, but
#                they are also not signing off, so it would overstate
#                the room to score it as 1.0.
#   dissent    — explicit opposition.
#
# An unrecognised stance scores as `supplement` rather than 0, so a
# future stance value can't silently drag consensus toward "total
# disagreement" before anyone notices the enum drifted.
#
# `abstain` is the one deliberate exception: it marks a turn where the
# persona produced no output at all (timeout / LLM error), so it says
# nothing about agreement. Those turns are excluded entirely —
# numerator AND denominator — rather than scored 0.5, which would
# drag every outage toward "the room half-agreed".
_STANCE_WEIGHT: dict[str, float] = {
    "agree": 1.0,
    "supplement": 0.5,
    "dissent": 0.0,
}
_UNKNOWN_STANCE_WEIGHT = 0.5
_ABSTAIN_STANCE = "abstain"


def observed_consensus(turns: Iterable[_Turn]) -> float | None:
    """Round-weighted agreement level across a transcript, in [0, 1].

    Returns None when there are no scorable turns — an absent value is
    honest, where 0.0 would read as "they all disagreed".

    Later rounds weigh more (weight = round number). A discussion opens
    with everyone staking out a position and converges as it goes, so
    round 1's spread of opinion says less about the conclusion than
    round 5's does. Weighting linearly rather than, say, taking only the
    final round keeps a late lone dissent from swinging the whole
    number, which matters at this scale (5 rounds, ~8 personas).
    """
    weighted_sum = 0.0
    weight_total = 0.0
    for t in turns:
        try:
            rnd = int(getattr(t, "round", 0) or 0)
        except (TypeError, ValueError):
            continue
        if rnd <= 0:
            continue
        stance = getattr(t, "stance", None)
        if stance == _ABSTAIN_STANCE:
            continue
        value = _STANCE_WEIGHT.get(
            stance if isinstance(stance, str) else "", _UNKNOWN_STANCE_WEIGHT,
        )
        weighted_sum += value * rnd
        weight_total += rnd

    if weight_total <= 0:
        return None
    return round(weighted_sum / weight_total, 4)


def consensus_gap(
    self_reported: Any, observed: float | None,
) -> float | None:
    """`self_reported - observed`, or None when either is unavailable.

    Positive means the synthesizer claimed more agreement than the
    transcript shows — the direction seen in production, and the one
    worth alerting on.
    """
    if observed is None:
        return None
    try:
        claimed = float(self_reported)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= claimed <= 1.0:
        return None
    return round(claimed - observed, 4)
