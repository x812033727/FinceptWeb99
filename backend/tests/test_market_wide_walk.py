"""Tests for tasks._market_wide — the single-date market-wide walk."""
from datetime import date

import pytest

from tasks._market_wide import (
    collect_market_wide,
    pending_days_since_newest,
    pending_market_days,
)


def test_walks_every_weekday_in_the_window():
    days = pending_market_days(date(2026, 7, 15), 7)

    assert days == [
        date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10),
        date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15),
    ]


def test_skips_weekends():
    days = pending_market_days(date(2026, 7, 13), 4)  # Mon, back to Thu

    assert date(2026, 7, 11) not in days  # Saturday
    assert date(2026, 7, 12) not in days  # Sunday
    assert days == [date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13)]


def test_skips_days_already_archived():
    """Steady state is one call for today; a gap heals itself."""
    have = {date(2026, 7, 13), date(2026, 7, 14)}

    days = pending_market_days(date(2026, 7, 15), 7, have)

    assert date(2026, 7, 13) not in days
    assert date(2026, 7, 14) not in days
    assert date(2026, 7, 15) in days
    assert date(2026, 7, 9) in days  # the gap still gets picked up


def test_nothing_pending_when_the_window_is_archived():
    have = {date(2026, 7, d) for d in (8, 9, 10, 13, 14, 15)}

    assert pending_market_days(date(2026, 7, 15), 7, have) == []


@pytest.mark.asyncio
async def test_collect_concatenates_one_fetch_per_day():
    seen = []

    async def _fetch(day_iso):
        seen.append(day_iso)
        return [{"date": day_iso, "n": 1}]

    rows = await collect_market_wide(_fetch, [date(2026, 7, 14), date(2026, 7, 13)])

    assert seen == ["2026-07-13", "2026-07-14"], "must ask oldest first"
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_collect_lets_errors_propagate():
    """Half-fetching and reporting success is the bug this module undoes."""
    async def _fetch(day_iso):
        if day_iso == "2026-07-14":
            raise RuntimeError("upstream said no")
        return [{"date": day_iso}]

    with pytest.raises(RuntimeError):
        await collect_market_wide(_fetch, [date(2026, 7, 13), date(2026, 7, 14)])


def test_since_newest_walks_the_whole_window_when_nothing_archived():
    """A cold table backfills."""
    days = pending_days_since_newest(date(2026, 7, 15), 7)

    assert days[0] == date(2026, 7, 8)
    assert days[-1] == date(2026, 7, 15)


def test_since_newest_anchors_on_the_latest_publication():
    """Weekly data never archives its non-publication weekdays, so
    `pending_market_days` would re-ask them on every run forever. This
    asks only what came after the last publication."""
    have = {date(2026, 7, 3), date(2026, 7, 9)}

    days = pending_days_since_newest(date(2026, 7, 15), 60, have)

    assert days == [
        date(2026, 7, 10), date(2026, 7, 13),
        date(2026, 7, 14), date(2026, 7, 15),
    ]
    assert date(2026, 7, 6) not in days, "must not re-ask a pre-anchor gap"


def test_since_newest_still_catches_a_shifted_publication():
    """Publication slips off its usual Friday around holidays — the new
    date is still after the anchor, so it's still asked for."""
    have = {date(2026, 7, 9)}

    days = pending_days_since_newest(date(2026, 7, 16), 60, have)

    assert date(2026, 7, 16) in days
    assert all(d > date(2026, 7, 9) for d in days)


def test_since_newest_is_empty_when_the_anchor_is_today():
    assert pending_days_since_newest(date(2026, 7, 15), 60, {date(2026, 7, 15)}) == []
