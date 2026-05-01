"""Tests for `tasks.score_news_sentiment`'s health-record formatting.

The cron's `_format_status` is the diagnostic surface admins read on
the IngestHealthCard — distinguishing the three indistinguishable
`scored=0` cases (nothing pending / cap hit / LLM unparseable) is the
whole point. Lock those messages in so a future refactor doesn't
silently reduce them back to "scored=0".
"""
from tasks.score_news_sentiment import _format_status


def test_status_nothing_pending():
    out = _format_status({"considered": 0, "scored": 0, "batches": 0, "cap_hit": 0})
    assert "considered=0" in out
    assert "nothing pending" in out


def test_status_cap_hit():
    out = _format_status(
        {"considered": 15, "scored": 8, "batches": 1, "cap_hit": 1},
    )
    assert "considered=15" in out
    assert "scored=8" in out
    assert "cap_hit" in out
    assert "SENTIMENT_DAILY_LLM_CALL_CAP" in out


def test_status_llm_unparseable():
    """`considered>0, scored=0, cap_hit=false` is the LLM-returned-junk
    failure mode — different cause from cap_hit but same `row_count=0`
    on the badge. The status text has to call it out so admins know to
    check the provider key + model, not the cap."""
    out = _format_status(
        {"considered": 12, "scored": 0, "batches": 1, "cap_hit": 0},
    )
    assert "considered=12" in out
    assert "scored=0" in out
    assert "unparseable" in out


def test_status_normal_success():
    out = _format_status(
        {"considered": 20, "scored": 20, "batches": 1, "cap_hit": 0},
    )
    assert "considered=20" in out
    assert "scored=20" in out
    assert "batches=1" in out
    # Success path doesn't surface diagnostic hints.
    assert "unparseable" not in out
    assert "cap_hit" not in out


def test_status_handles_missing_keys_defensively():
    """A future refactor of `score_pending`'s return shape mustn't take
    out the cron's status line. Missing keys should default to 0."""
    out = _format_status({})
    assert "considered=0" in out
