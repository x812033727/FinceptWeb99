"""Correct abstention counts as a hit (spec Part 2).

hit_count only-on-win is why zero lessons have EVER promoted across two
generations of the stack (max ratio 0.259 vs the 0.6 floor): a system
that correctly sits out falling tapes had its best behaviour scored as
failure. An abstention over a pool that then FELL is a vindicated call.
"""
import pytest

from services.lesson_tier_service import qualifies_for_hit


@pytest.mark.parametrize("verdict,pool,expected", [
    ("win", None, True),
    ("big_win", 2.0, True),
    ("abstain", -1.7, True),      # pool fell — the caution was right
    ("abstain", 0.0, False),      # flat pool: not vindicated
    ("abstain", 3.2, False),      # pool rose: a missed gain, not a hit
    ("abstain", None, False),     # unmeasured pool: no evidence, no hit
    ("loss", -5.0, False),
    ("big_loss", -8.0, False),
    ("unverifiable", -1.0, False),
    (None, -1.0, False),
])
def test_qualifies_for_hit(verdict, pool, expected):
    assert qualifies_for_hit(verdict, pool) is expected
