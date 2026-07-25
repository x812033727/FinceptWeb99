"""An abstention (or an unusable conclusion) has no pick to grade, so it
must be due for verification immediately — not parked 5 trading days out
where it stays invisible on the scoreboard. A real recommendation still
waits for its D5 bars to resolve."""
from datetime import date

from services.discussion.synthesizer import _verify_after_date


def test_no_recommendation_is_due_immediately():
    anchor = date(2026, 7, 24)
    assert _verify_after_date({"abstained": True, "recommended_symbols": []}, anchor) == anchor


def test_empty_unusable_conclusion_is_due_immediately():
    anchor = date(2026, 7, 24)
    assert _verify_after_date({}, anchor) == anchor


def test_a_real_pick_waits_for_its_window():
    anchor = date(2026, 7, 24)
    out = _verify_after_date({"recommended_symbols": ["2330"]}, anchor)
    assert out > anchor
