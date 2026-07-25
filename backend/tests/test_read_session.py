"""Session resolver + archive-first wrapper for the daily discussion.

The resolver answers "which settled TW session should a live discussion
read from the archive". The wrapper implements archive-first-with-live-
fallback, where the fallback predicate is "did the archive answer FOR THE
REQUESTED SESSION" — an emptiness check is not enough because the archive
queries clamp `<= session` and silently return an older day when the
target is missing (the 11-day-stale broker-data abstention of 2026-05-20).
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from services.discussion.context.read_session import (
    archive_first,
    resolve_read_session,
)

TPE = ZoneInfo("Asia/Taipei")


# ── resolver: table-driven, never date.today() ─────────────────────

@pytest.mark.parametrize("now_tw, expected", [
    # 04:00 Friday — today's session hasn't traded: read Thursday.
    (datetime(2026, 7, 24, 4, 0, tzinfo=TPE), date(2026, 7, 23)),
    # 04:00 Monday — read the prior Friday.
    (datetime(2026, 7, 20, 4, 0, tzinfo=TPE), date(2026, 7, 17)),
    # 04:00 Saturday — read Friday.
    (datetime(2026, 7, 25, 4, 0, tzinfo=TPE), date(2026, 7, 24)),
    # 04:00 Sunday — still Friday.
    (datetime(2026, 7, 26, 4, 0, tzinfo=TPE), date(2026, 7, 24)),
    # 16:00 weekday — today's session has settled (post-14:30 publish).
    (datetime(2026, 7, 24, 16, 0, tzinfo=TPE), date(2026, 7, 24)),
    # 12:00 weekday — intraday, today not settled: read yesterday.
    (datetime(2026, 7, 24, 12, 0, tzinfo=TPE), date(2026, 7, 23)),
    # 16:00 Saturday — weekend afternoon still reads Friday.
    (datetime(2026, 7, 25, 16, 0, tzinfo=TPE), date(2026, 7, 24)),
])
def test_resolver_returns_most_recent_settled_session(now_tw, expected):
    assert resolve_read_session(now_tw) == expected


# ── wrapper ────────────────────────────────────────────────────────

def _answered(result):
    return result.get("session") if isinstance(result, dict) else None


@pytest.mark.asyncio
async def test_archive_answering_the_requested_session_wins():
    calls = []

    async def call(as_of):
        calls.append(as_of)
        return {"session": date(2026, 7, 23), "rows": [1]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "archive"
    assert calls == [date(2026, 7, 23)]      # live path never invoked
    assert result["rows"] == [1]


@pytest.mark.asyncio
async def test_stale_archive_answer_falls_back_to_live():
    """`<= session` clamping returns an OLDER day when the target is
    missing. That must count as a miss, or week-old data is served
    silently and the fallback never fires."""
    async def call(as_of):
        if as_of is not None:
            return {"session": date(2026, 7, 18), "rows": ["old"]}
        return {"session": date(2026, 7, 23), "rows": ["fresh"]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "live_fallback"
    assert result["rows"] == ["fresh"]


@pytest.mark.asyncio
async def test_empty_archive_falls_back_to_live():
    async def call(as_of):
        if as_of is not None:
            return []
        return {"session": date(2026, 7, 23), "rows": ["fresh"]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "live_fallback"
    assert result["rows"] == ["fresh"]


@pytest.mark.asyncio
async def test_max_lag_days_accepts_a_recent_enough_answer():
    """Non-daily series (fundamentals snapshots) declare a cadence;
    an answer within it is NOT a miss."""
    async def call(as_of):
        assert as_of is not None, "live path must not be reached"
        return {"session": date(2026, 7, 21), "rows": [1]}

    result, source = await archive_first(
        call, session=date(2026, 7, 23),
        answered_session=_answered, max_lag_days=5,
    )
    assert source == "archive"


@pytest.mark.asyncio
async def test_live_failure_keeps_the_stale_archive_answer():
    """A stale archive answer beats no answer: if the live fallback
    raises, serve the stale data and say so."""
    async def call(as_of):
        if as_of is not None:
            return {"session": date(2026, 7, 18), "rows": ["old"]}
        raise RuntimeError("upstream down")

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "archive_stale"
    assert result["rows"] == ["old"]


@pytest.mark.asyncio
async def test_both_paths_empty_raises_nothing_and_reports_fallback():
    async def call(as_of):
        return []

    result, source = await archive_first(
        call, session=date(2026, 7, 23), answered_session=_answered,
    )
    assert source == "live_fallback"
    assert result == []


@pytest.mark.asyncio
async def test_live_failure_with_empty_archive_reraises():
    """Nothing to serve — the exception must reach the block's
    record_error exactly as it does today. No new silent path."""
    async def call(as_of):
        if as_of is not None:
            return []
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await archive_first(
            call, session=date(2026, 7, 23), answered_session=_answered,
        )
