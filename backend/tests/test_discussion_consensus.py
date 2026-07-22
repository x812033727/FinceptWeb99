"""Unit tests for services.discussion.consensus.

Pure scoring rule, so these test it directly on stance sequences
rather than through a synthesized discussion.
"""
from dataclasses import dataclass

from services.discussion.consensus import consensus_gap, observed_consensus


@dataclass
class T:
    round: int
    stance: str


def test_all_agree_scores_one():
    assert observed_consensus([T(1, "agree"), T(2, "agree")]) == 1.0


def test_all_dissent_scores_zero():
    assert observed_consensus([T(1, "dissent"), T(2, "dissent")]) == 0.0


def test_supplement_is_partial_agreement():
    """A supplementing analyst isn't dissenting, but isn't signing off
    either — scoring it as full agreement would overstate the room."""
    assert observed_consensus([T(1, "supplement")]) == 0.5


def test_empty_transcript_is_none_not_zero():
    """Absent is honest; 0.0 would read as 'they all disagreed'."""
    assert observed_consensus([]) is None


def test_turns_without_a_round_are_skipped():
    """User-injected directives and malformed rows carry round 0 and
    must not drag the score."""
    assert observed_consensus([T(0, "dissent"), T(1, "agree")]) == 1.0


def test_later_rounds_weigh_more():
    """Same stance counts, opposite order. A discussion converges, so
    where the agreement sits in the transcript changes the answer —
    if it didn't, the round weighting would be doing nothing."""
    early_dissent = observed_consensus([T(1, "dissent"), T(2, "agree")])
    late_dissent = observed_consensus([T(1, "agree"), T(2, "dissent")])
    assert early_dissent is not None and late_dissent is not None
    assert early_dissent > late_dissent
    # 1*0 + 2*1 over weight 3
    assert early_dissent == round(2 / 3, 4)


def test_unknown_stance_scores_as_supplement():
    """A new stance value must not silently push consensus to zero
    before anyone notices the enum drifted."""
    assert observed_consensus([T(1, "brand_new_stance")]) == 0.5


def test_gap_is_positive_when_the_model_overclaims():
    """The production signature: consensus_score 0.88 over a
    0-agree / 36-dissent transcript."""
    observed = observed_consensus([T(1, "dissent")] * 36)
    assert observed == 0.0
    assert consensus_gap(0.88, observed) == 0.88


def test_gap_is_none_when_either_side_is_missing():
    assert consensus_gap(0.5, None) is None
    assert consensus_gap(None, 0.5) is None
    assert consensus_gap("not a number", 0.5) is None


def test_gap_rejects_out_of_range_self_reports():
    """`_safe_conclusion` clamps, but this helper also reads raw
    conclusions from older rows — an 88 (percent) must not be treated
    as a valid 0-1 score."""
    assert consensus_gap(88, 0.5) is None
