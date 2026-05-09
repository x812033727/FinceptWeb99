"""Tests for `tasks.score_news_sentiment`'s combined news + announcement
status formatting.

PR-D1b extended the cron to drain BOTH the news archive and the
corporate-announcements archive in one pass, sharing the daily
LLM-call cap. The single-line status the cron writes to
IngestHealthCard now has to distinguish four states across two
lanes — these tests pin the operator-visible diagnostics so a
future refactor doesn't silently regress them.
"""
from tasks.score_news_sentiment import _format_combined_status


def test_status_both_lanes_healthy():
    """Normal day: both lanes drained some rows."""
    out = _format_combined_status(
        {"considered": 20, "scored": 20, "batches": 1, "cap_hit": 0},
        {"considered": 8, "scored": 8, "batches": 1, "cap_hit": 0},
    )
    assert "news=20/20 (1b)" in out
    assert "ann=8/8 (1b)" in out
    assert "cap_hit" not in out
    assert "err=" not in out


def test_status_both_lanes_empty():
    """Backlog drained — news + ann both empty for this tick. The
    zeros tell the story, no extra suffix needed."""
    out = _format_combined_status(
        {"considered": 0, "scored": 0, "batches": 0},
        {"considered": 0, "scored": 0, "batches": 0},
    )
    assert "news=0/0" in out
    assert "ann=0/0" in out
    assert "cap_hit" not in out


def test_status_news_lane_hit_cap():
    """News pass exhausted the shared daily cap. Announcements got
    nothing (the cap counter was already exhausted by the time the
    second lane checked)."""
    out = _format_combined_status(
        {"considered": 80, "scored": 40, "batches": 4, "cap_hit": 1},
        {"considered": 0, "scored": 0, "batches": 0, "cap_hit": 1},
    )
    assert "news=80/40 (4b)" in out
    assert "ann=0/0 (0b)" in out
    assert "cap_hit" in out
    assert "daily LLM limit" in out


def test_status_announcement_lane_unparseable():
    """News scored fine; announcements LLM returned garbage. The
    err suffix surfaces the announcement lane's diagnostic so admins
    can spot a category-specific prompt failure (e.g. MOPS title
    contains a quote that broke the JSON)."""
    out = _format_combined_status(
        {"considered": 20, "scored": 20, "batches": 1, "cap_hit": 0},
        {
            "considered": 5, "scored": 0, "batches": 0, "cap_hit": 0,
            "error": "llm response did not parse as JSON array; raw=...",
        },
    )
    assert "news=20/20" in out
    assert "ann=5/0" in out
    assert "err=" in out
    assert "did not parse" in out


def test_status_handles_missing_keys_defensively():
    """A future refactor of `score_pending`'s return shape mustn't
    take out the cron's status line. Missing keys default to 0."""
    out = _format_combined_status({}, {})
    assert "news=0/0 (0b)" in out
    assert "ann=0/0 (0b)" in out


def test_status_surfaces_provider_cooldown_distinct_from_cap_hit():
    """When a lane is in provider failure cooldown the status must
    say `cooldown (provider_name)` — distinct from `cap_hit`. Both
    halt scoring but for different reasons + different timelines:
    cap_hit resets at UTC midnight, cooldown resets after the
    backoff TTL (1h-6h)."""
    out = _format_combined_status(
        {
            "considered": 0, "scored": 0, "batches": 0,
            "cap_hit": 0, "cooldown_active": 1, "provider": "anthropic",
        },
        {"considered": 0, "scored": 0, "batches": 0, "cap_hit": 0},
    )
    assert "cooldown (anthropic)" in out
    assert "cap_hit" not in out


def test_status_renders_cap_hit_and_cooldown_together():
    """Both states can co-occur — cap exhausted AND a separate provider
    in cooldown. Both must surface so the operator sees the full
    picture rather than only the first-encountered halt reason."""
    out = _format_combined_status(
        {
            "considered": 80, "scored": 40, "batches": 2,
            "cap_hit": 1, "cooldown_active": 0,
        },
        {
            "considered": 0, "scored": 0, "batches": 0,
            "cap_hit": 0, "cooldown_active": 1, "provider": "anthropic",
        },
    )
    assert "cap_hit" in out
    assert "cooldown (anthropic)" in out
