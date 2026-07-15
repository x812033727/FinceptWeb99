from datetime import UTC, date, datetime

import pytest

from services import market_calendar_service as calendar


def test_nyse_holidays_follow_exchange_rules():
    holidays = calendar.nyse_holidays(2026)
    assert len(holidays) == 10
    assert date(2026, 1, 19) in holidays
    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 7, 3) in holidays  # Independence Day observed
    assert date(2026, 11, 26) in holidays


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 1, 19, 15, tzinfo=UTC),
        datetime(2026, 4, 3, 15, tzinfo=UTC),
        datetime(2026, 7, 3, 15, tzinfo=UTC),
    ],
)
def test_nyse_official_holidays_are_closed(at):
    session = calendar.market_session("US", at)
    assert session.is_open is False
    assert session.reason == "holiday"
    assert session.calendar_source == "NYSE rules"


def test_nyse_announced_early_close_uses_eastern_time():
    before_close = calendar.market_session("US", datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
    at_close = calendar.market_session("US", datetime(2026, 11, 27, 18, tzinfo=UTC))
    assert before_close.is_open is True
    assert before_close.closes_at is not None
    assert before_close.closes_at.hour == 13
    assert before_close.calendar_source == "NYSE announced early close"
    assert at_close.is_open is False
    assert at_close.reason == "after_close"


def test_twse_official_holidays_include_non_trading_settlement_days():
    lunar_settlement = calendar.market_session("TW", datetime(2026, 2, 12, 2, tzinfo=UTC))
    teacher_day = calendar.market_session("TW", datetime(2026, 9, 28, 2, tzinfo=UTC))
    resumed = calendar.market_session("TW", datetime(2026, 2, 23, 2, tzinfo=UTC))
    assert lunar_settlement.is_open is False
    assert lunar_settlement.reason == "holiday"
    assert lunar_settlement.calendar_source == "TWSE 2026 official calendar"
    assert teacher_day.is_open is False
    assert resumed.is_open is True


def test_session_reports_weekends_boundaries_and_calendar_fallback():
    weekend = calendar.market_session("US", datetime(2026, 7, 18, 15, tzinfo=UTC))
    before_open = calendar.market_session("TW", datetime(2027, 7, 15, 0, tzinfo=UTC))
    assert weekend.reason == "weekend"
    assert before_open.reason == "before_open"
    assert before_open.calendar_source == "TWSE weekday fallback"


def test_crypto_is_continuous_and_invalid_inputs_fail_closed():
    crypto = calendar.market_session("CRYPTO", datetime(2026, 7, 18, 6, tzinfo=UTC))
    assert crypto.is_open is True
    assert crypto.reason == "continuous"
    assert crypto.opens_at is None and crypto.closes_at is None
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.market_session("US", datetime(2026, 7, 15, 14))
    with pytest.raises(ValueError, match="unsupported market"):
        calendar.market_session("XX", datetime(2026, 7, 15, 14, tzinfo=UTC))


def test_next_market_close_skips_holidays_and_honors_early_close():
    before_mlk_weekend = datetime(2026, 1, 16, 22, tzinfo=UTC)
    assert calendar.next_market_close("US", before_mlk_weekend) == datetime(
        2026, 1, 20, 21, tzinfo=UTC
    )

    before_early_close = datetime(2026, 11, 27, 14, tzinfo=UTC)
    assert calendar.next_market_close("US", before_early_close) == datetime(
        2026, 11, 27, 18, tzinfo=UTC
    )
    assert calendar.next_market_close("US", datetime(2026, 11, 27, 18, tzinfo=UTC)) == datetime(
        2026, 11, 30, 21, tzinfo=UTC
    )


def test_next_market_close_handles_twse_lunar_break_and_crypto_day():
    assert calendar.next_market_close("TW", datetime(2026, 2, 11, 6, tzinfo=UTC)) == datetime(
        2026, 2, 23, 5, 30, tzinfo=UTC
    )
    assert calendar.next_market_close("CRYPTO", datetime(2026, 12, 31, 23, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.next_market_close("US", datetime(2026, 7, 15, 14))
