"""Trading-day-aware data-freshness helper for the ingest health UI.

The legacy "data stale" check counted plain calendar days between
`last_run_at` and `latest_data_ts`. That false-fires every Monday
morning for TW + US ingests because Sat/Sun aren't trading days —
the data is two calendar days behind, but zero trading days behind.
Long weekends + national holidays compound the issue.

This module provides a simple weekday-counting helper that the
admin-health endpoint applies before serializing each row. The
front-end then renders a single boolean instead of re-implementing
the calendar in TypeScript (which would also need a holiday list
shipped to the browser).

Intentional simplifications:

  - "Trading day" == weekday (Mon-Fri). TW national holidays + NYSE
    holidays are NOT subtracted. Net effect: the staleness rule is
    *more lenient* on holiday weeks (one extra "non-trading" day
    we don't subtract = one extra grace day). False negatives are
    cheap — operator misses one stale day. False positives over a
    long weekend, by contrast, were noisy enough to cause alert
    fatigue, which is what motivated this change.

  - Only counts FULL trading days strictly between the two
    timestamps. A run at 14:30 UTC Monday with `latest_data_ts =
    Friday's date` is "1 trading day behind" — Monday hasn't
    completed yet but Tuesday/Wed/Thu wouldn't be counted either
    because the run hasn't seen them. This matches operator
    intuition: a Monday-morning ingest reading Friday's data is
    one trading day behind, not zero.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


# Default tolerance window for "data stale". Mirrors the previous
# 2-calendar-day rule but in trading-day units, so a long weekend +
# Mon holiday no longer false-fires.
DEFAULT_STALE_TRADING_DAYS = 2


def _weekdays_strictly_between(start: date, end: date) -> int:
    """Count Mon-Fri days strictly between `start` and `end` (both
    exclusive). Returns 0 when `end <= start`. Range is at most a
    few days for typical staleness checks, so the linear walk is
    cheap — no need for the math-based shortcut.
    """
    if end <= start:
        return 0
    cur = start + timedelta(days=1)
    count = 0
    while cur < end:
        if cur.weekday() < 5:
            count += 1
        cur = cur + timedelta(days=1)
    return count


def is_data_stale(
    *,
    last_run_at: str | datetime | None,
    latest_data_ts: str | date | datetime | None,
    max_trading_days: int = DEFAULT_STALE_TRADING_DAYS,
) -> bool:
    """Return True when the run is more than `max_trading_days`
    weekday-only days ahead of the latest written data point.

    Both timestamps accept ISO-8601 strings (the wire format from
    `record_health`'s JSON payload), Python `date`, or `datetime`.
    Either being missing returns False — non-time-series tasks
    (verify, score, prune, snapshot) leave `latest_data_ts` null
    and shouldn't render a stale badge at all.
    """
    if last_run_at is None or latest_data_ts is None:
        return False

    run_dt = _coerce_datetime(last_run_at)
    data_dt = _coerce_datetime(latest_data_ts)
    if run_dt is None or data_dt is None:
        return False

    behind = _weekdays_strictly_between(data_dt.date(), run_dt.date())
    return behind > max_trading_days


def _coerce_datetime(value: str | date | datetime) -> datetime | None:
    """Best-effort parse of a timestamp into an aware UTC datetime.

    Returns None on parse failure so the caller falls back to "not
    stale" — better than 500-ing the admin endpoint on a malformed
    Redis payload.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            try:
                parsed = datetime.combine(
                    date.fromisoformat(value[:10]),
                    datetime.min.time(),
                    tzinfo=UTC,
                )
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
