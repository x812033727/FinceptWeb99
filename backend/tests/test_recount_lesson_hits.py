"""Recount is a from-scratch aggregation, not an increment replay —
idempotent by construction, so running it twice cannot double-count."""
from scripts.recount_lesson_hits import aggregate_hits, would_promote


def test_aggregate_counts_once_per_qualifying_discussion():
    # (discussion qualifies?, lesson_ids cited)
    rows = [
        (True,  {1, 2}),
        (True,  {2}),
        (False, {1, 2, 3}),
    ]
    assert aggregate_hits(rows) == {1: 1, 2: 2}


def test_would_promote_applies_both_floors():
    # (usage, hits)
    assert would_promote(usage=10, hits=6) is True     # 0.6 exactly
    assert would_promote(usage=10, hits=5) is False    # ratio floor
    assert would_promote(usage=4, hits=4) is False     # usage floor
    assert would_promote(usage=0, hits=0) is False
