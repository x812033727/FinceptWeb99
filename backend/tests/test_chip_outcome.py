"""Holiday-safe outcome classification for the chip ingest walk.

The naive rule "past day empty = gap" would fire on every holiday
(holidays stay pending forever by design). The witness is our own
price archive: if ohlcv_daily has TW bars for that day, the market
traded — chips missing is a real gap, not a holiday.
"""
from datetime import date

from tasks.chip_outcome import classify_chip_outcome

TODAY = date(2026, 7, 25)


def test_past_traded_day_empty_is_gap():
    ok, status = classify_chip_outcome(
        day_rows={date(2026, 7, 24): 0, TODAY: 0},
        today=TODAY,
        traded={date(2026, 7, 24)},
    )
    assert ok is False
    assert status is not None and status.startswith("gap: 2026-07-24")


def test_today_empty_is_not_yet_published():
    ok, status = classify_chip_outcome(
        day_rows={TODAY: 0}, today=TODAY, traded=set(),
    )
    assert ok is True
    assert status == "not_yet_published: 2026-07-25"


def test_past_holiday_empty_is_quiet():
    # 7-21 was a (hypothetical) holiday: no price bars → not a gap.
    ok, status = classify_chip_outcome(
        day_rows={date(2026, 7, 21): 0, date(2026, 7, 24): 1300},
        today=TODAY,
        traded={date(2026, 7, 24)},
    )
    assert ok is True
    assert status is None


def test_rows_written_clean():
    ok, status = classify_chip_outcome(
        day_rows={date(2026, 7, 24): 1300},
        today=TODAY,
        traded={date(2026, 7, 24)},
    )
    assert (ok, status) == (True, None)


def test_nothing_fetched_is_idle():
    ok, status = classify_chip_outcome(day_rows={}, today=TODAY, traded=set())
    assert (ok, status) == (True, "idle: nothing due")
