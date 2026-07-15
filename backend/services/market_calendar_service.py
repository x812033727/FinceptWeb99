"""Exchange-aware core trading sessions for paper-order execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketSession:
    market: str
    local_date: date
    opens_at: datetime | None
    closes_at: datetime | None
    is_open: bool
    reason: str
    calendar_source: str


_NY = ZoneInfo("America/New_York")
_TW = ZoneInfo("Asia/Taipei")

# Published by TWSE for 2026. Weekend rows and informational first/last
# trading-day rows are intentionally omitted.
# https://www.twse.com.tw/en/trading/holiday.html
_TWSE_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset(
        date.fromisoformat(value)
        for value in (
            "2026-01-01",
            "2026-02-12",
            "2026-02-13",
            "2026-02-16",
            "2026-02-17",
            "2026-02-18",
            "2026-02-19",
            "2026-02-20",
            "2026-02-27",
            "2026-04-03",
            "2026-04-06",
            "2026-05-01",
            "2026-06-19",
            "2026-09-25",
            "2026-09-28",
            "2026-10-09",
            "2026-10-26",
            "2026-12-25",
        )
    )
}

# NYSE has announced these 13:00 ET closes. Keeping exceptional closes
# explicit prevents inferred rules from diverging from the exchange notice.
# https://www.nyse.com/trade/hours-calendars
_NYSE_EARLY_CLOSES = frozenset(
    date.fromisoformat(value)
    for value in (
        "2026-11-27",
        "2026-12-24",
        "2027-11-26",
        "2028-07-03",
        "2028-11-24",
    )
)


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_year = year + 1 if month == 12 else year
    first_next_month = date(next_year, month % 12 + 1, 1)
    candidate = first_next_month - timedelta(days=1)
    return candidate - timedelta(days=(candidate.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter using the Anonymous Gregorian computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    month_seed = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * month_seed) // 451
    month = (h + month_seed - 7 * m + 114) // 31
    day = (h + month_seed - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed_fixed(day: date, *, preceding_friday: bool = True) -> date:
    if day.weekday() == 5 and preceding_friday:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observed_fixed(date(year, 1, 1), preceding_friday=False),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed_fixed(date(year, 6, 19)),
        _observed_fixed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed(date(year, 12, 25)),
    }
    return frozenset(day for day in holidays if day.year == year)


def market_session(market: str, at: datetime) -> MarketSession:
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    if market == "CRYPTO":
        local = at.astimezone(ZoneInfo("UTC"))
        return MarketSession(
            market=market,
            local_date=local.date(),
            opens_at=None,
            closes_at=None,
            is_open=True,
            reason="continuous",
            calendar_source="24/7",
        )
    if market not in {"US", "TW"}:
        raise ValueError(f"unsupported market {market}")

    timezone = _NY if market == "US" else _TW
    local = at.astimezone(timezone)
    local_day = local.date()
    opens = time(9, 30) if market == "US" else time(9)
    closes = time(13, 30) if market == "TW" else time(16)
    source = "NYSE rules"
    if market == "TW":
        source = (
            f"TWSE {local_day.year} official calendar"
            if local_day.year in _TWSE_HOLIDAYS
            else "TWSE weekday fallback"
        )

    if local_day.weekday() >= 5:
        reason = "weekend"
    elif market == "US" and local_day in nyse_holidays(local_day.year):
        reason = "holiday"
    elif market == "TW" and local_day in _TWSE_HOLIDAYS.get(local_day.year, ()):
        reason = "holiday"
        source = f"TWSE {local_day.year} official calendar"
    else:
        reason = "regular"

    if market == "US" and local_day in _NYSE_EARLY_CLOSES:
        closes = time(13)
        source = "NYSE announced early close"

    opens_at = datetime.combine(local_day, opens, timezone)
    closes_at = datetime.combine(local_day, closes, timezone)
    if reason in {"weekend", "holiday"}:
        is_open = False
    else:
        is_open = opens_at <= local < closes_at
        if not is_open:
            reason = "before_open" if local < opens_at else "after_close"
    return MarketSession(
        market=market,
        local_date=local_day,
        opens_at=opens_at,
        closes_at=closes_at,
        is_open=is_open,
        reason=reason,
        calendar_source=source,
    )


def is_market_open(market: str, at: datetime) -> bool:
    return market_session(market, at).is_open
