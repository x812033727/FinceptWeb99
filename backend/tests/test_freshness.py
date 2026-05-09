"""Trading-day-aware staleness helper unit tests.

The legacy 2-calendar-day rule false-fired every Monday morning. This
module verifies the new helper subtracts weekends so:
  - run Mon ↔ data Fri = 0 trading days behind (NOT stale)
  - run Mon ↔ data prev-Tue = 3 trading days behind (stale)
  - run Tue ↔ data prev-Tue = 5 trading days behind (stale)
"""
from datetime import date, datetime

from services.freshness import (
    DEFAULT_STALE_TRADING_DAYS,
    _weekdays_strictly_between,
    is_data_stale,
)


# Calendar reference for tests below. Days of week:
#   2026-04-24  Fri
#   2026-04-25  Sat
#   2026-04-26  Sun
#   2026-04-27  Mon
#   2026-04-28  Tue
#   2026-04-29  Wed
#   2026-04-30  Thu
#   2026-05-01  Fri
#   2026-05-02  Sat
#   2026-05-03  Sun
#   2026-05-04  Mon

def test_weekdays_strictly_between_subtracts_weekends():
    # Fri → Mon: only weekend days in between, both excluded.
    assert _weekdays_strictly_between(date(2026, 4, 24), date(2026, 4, 27)) == 0
    # Mon → Tue: zero weekdays strictly between.
    assert _weekdays_strictly_between(date(2026, 4, 27), date(2026, 4, 28)) == 0
    # Mon → Fri: 3 weekdays strictly between (Tue + Wed + Thu).
    assert _weekdays_strictly_between(date(2026, 4, 27), date(2026, 5, 1)) == 3
    # Returns 0 for inverted / equal range.
    assert _weekdays_strictly_between(date(2026, 5, 4), date(2026, 4, 27)) == 0
    assert _weekdays_strictly_between(date(2026, 4, 27), date(2026, 4, 27)) == 0


def test_is_data_stale_returns_false_when_latest_ts_is_missing():
    """Non-time-series tasks (verify, score, prune) never have
    latest_data_ts — they should never render a stale badge."""
    assert is_data_stale(
        last_run_at="2026-05-04T08:00:00+00:00",
        latest_data_ts=None,
    ) is False


def test_is_data_stale_returns_false_for_monday_with_friday_data():
    """The headline regression case: weekend ≠ stale.

    Old rule: 3 calendar days between Fri and Mon → stale.
    New rule: 0 trading days strictly between → not stale.
    """
    assert is_data_stale(
        last_run_at="2026-04-27T08:00:00+00:00",   # Mon morning
        latest_data_ts="2026-04-24",                # Fri's data
    ) is False


def test_is_data_stale_returns_false_for_tuesday_with_monday_data():
    """One trading day behind is well within the 2-day budget."""
    assert is_data_stale(
        last_run_at="2026-04-28T08:00:00+00:00",   # Tue
        latest_data_ts="2026-04-27",                # Mon's data
    ) is False


def test_is_data_stale_returns_true_when_three_trading_days_behind():
    """Run on Friday with data from prev-Mon = Tue + Wed + Thu = 3
    trading days behind, exceeds the default 2-day budget."""
    assert is_data_stale(
        last_run_at="2026-05-01T08:00:00+00:00",   # Fri
        latest_data_ts="2026-04-27",                # prev Mon
    ) is True


def test_is_data_stale_handles_long_weekend_holiday_leniently():
    """A 4-day weekend (Fri holiday + Sat + Sun + Mon holiday) reading
    Thu's data on Tue counts as 0 trading days behind under the
    weekday-only rule. Holidays are NOT subtracted, which means we
    err on the side of "not stale" for holiday weeks — this is the
    deliberate trade to avoid alert fatigue."""
    assert is_data_stale(
        last_run_at="2026-04-28T08:00:00+00:00",   # Tue
        latest_data_ts="2026-04-23",                # prev Thu
    ) is False


def test_is_data_stale_respects_explicit_threshold():
    """Tighter budget (1 trading day) catches what the default 2-day
    budget allows. Used by tasks that should be more sensitive."""
    # Tue run with prev-Fri data = 1 trading day behind (Mon).
    args = {
        "last_run_at": "2026-04-28T08:00:00+00:00",
        "latest_data_ts": "2026-04-24",
    }
    assert is_data_stale(**args) is False
    assert is_data_stale(**args, max_trading_days=0) is True


def test_is_data_stale_accepts_date_and_datetime_objects():
    """Accept native types alongside ISO strings — used when the API
    layer has already parsed the payload."""
    assert is_data_stale(
        last_run_at=datetime(2026, 5, 1, 8, 0),     # Fri
        latest_data_ts=date(2026, 4, 27),            # prev Mon
    ) is True


def test_is_data_stale_returns_false_on_malformed_input():
    """Don't 500 the admin endpoint on a junk payload — fall back
    to "not stale" so the operator sees the row + error string and
    can act on the underlying badge."""
    assert is_data_stale(
        last_run_at="not-a-date",
        latest_data_ts="2026-04-24",
    ) is False
    assert is_data_stale(
        last_run_at="2026-05-01T08:00:00+00:00",
        latest_data_ts="garbage",
    ) is False


def test_default_threshold_is_2_trading_days():
    """Pin the constant — operator-facing threshold change should be a
    deliberate edit, not a silent regression."""
    assert DEFAULT_STALE_TRADING_DAYS == 2
