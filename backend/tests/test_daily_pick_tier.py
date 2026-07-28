"""Read-time confidence tier. Presentation only: the 20-decided rule
guards picking, and this function never touches picking."""
import pytest

from services.daily_pick_tier import T_CONSENSUS, T_HALLUC, tier_for


def _conclusion(consensus=0.9, warnings=0, symbols=("2330",)):
    return {
        "recommended_symbols": list(symbols),
        "consensus_score": consensus,
        "quality_signals": {
            "hallucination_warnings": [{"round": 1}] * warnings,
        },
    }


def test_high_consensus_low_warnings_is_recommend():
    assert tier_for(_conclusion(consensus=0.9, warnings=0)) == "recommend"


def test_threshold_boundary_is_inclusive():
    assert tier_for(_conclusion(consensus=T_CONSENSUS, warnings=T_HALLUC)) == "recommend"


def test_low_consensus_is_watch():
    assert tier_for(_conclusion(consensus=T_CONSENSUS - 0.01)) == "watch"


def test_warning_heavy_is_watch():
    assert tier_for(_conclusion(warnings=T_HALLUC + 1)) == "watch"


def test_abstention_has_no_tier():
    assert tier_for({"recommended_symbols": [], "abstained": True}) is None


@pytest.mark.parametrize("broken", [
    None,
    {},
    {"recommended_symbols": ["2330"]},                     # no consensus_score
    {"recommended_symbols": ["2330"], "consensus_score": "high"},  # wrong type
    {"recommended_symbols": ["2330"], "consensus_score": 0.9,
     "quality_signals": "corrupt"},
])
def test_malformed_never_crashes_and_never_recommends(broken):
    assert tier_for(broken) in (None, "watch")
