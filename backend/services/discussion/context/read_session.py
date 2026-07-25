"""Archive-first reads for the live daily discussion.

The daily discussion runs at 04:00 Taipei, hours after every TW ingest
job finished writing the previous session into the database — yet live
mode re-fetched everything over HTTP. These two helpers close that gap:

- `resolve_read_session` answers "which settled session should a live
  run read". At 04:00 that is the previous trading day.
- `archive_first` calls an existing dual-mode service with
  `as_of=<session>` first and falls back to the live path only when the
  archive's answer is missing or STALE. Staleness matters because the
  archive queries clamp `<= session`: when the target day is missing
  they return an older day rather than nothing, and serving that
  silently is exactly how a panel ends up abstaining against 11-day-old
  broker data (2026-05-20, see the design doc).

The builder's own `as_of` stays None — the discussion row must remain
live-classified (no 回測 badge, no backtest gating, no extra day of
clamping).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from services.tw_trading_calendar import prev_trading_day_estimate

log = logging.getLogger(__name__)

# TWSE publishes the settled session (STOCK_DAY_ALL) around 14:30
# Taipei; 15:00 leaves margin for slow publish days.
_SESSION_SETTLED_HOUR = 15


def resolve_read_session(now_tw: datetime | None = None) -> date:
    """The most recent settled TW trading session as of `now_tw`.

    Before 15:00 Taipei (or on a weekend) that is the previous trading
    day; from 15:00 on a weekday it is the day itself. Holidays are
    handled downstream: the archive clamps `<= session`, so an estimate
    landing on a holiday resolves to the prior real session — which the
    staleness predicate in `archive_first` must therefore tolerate via
    the caller-declared `max_lag_days` when it matters.
    """
    if now_tw is None:
        now_tw = datetime.now(ZoneInfo("Asia/Taipei"))
    today = now_tw.date()
    if today.weekday() < 5 and now_tw.hour >= _SESSION_SETTLED_HOUR:
        return today
    return prev_trading_day_estimate(today)


async def archive_first(
    call: Callable[[date | None], Awaitable[Any]],
    *,
    session: date,
    answered_session: Callable[[Any], date | None],
    max_lag_days: int = 0,
) -> tuple[Any, str]:
    """Call `call(session)`; fall back to `call(None)` when the archive
    did not answer for the requested session.

    Returns `(result, source)` with source one of:
      - "archive"       — archive answered within `max_lag_days` of
                          `session`
      - "live_fallback" — archive missing/stale, live path answered
                          (or both were empty; empty live result is
                          returned as-is for the block's existing
                          empty-handling)
      - "archive_stale" — archive was stale AND the live path failed;
                          the stale answer is served because stale
                          beats blind (blind spots cause abstention)

    A live-path exception with nothing usable from the archive is
    re-raised so the block's `record_error` fires exactly as today.
    """
    archive_result = await call(session)
    answered = answered_session(archive_result) if archive_result else None
    floor = session - timedelta(days=max_lag_days)
    if answered is not None and answered >= floor:
        return archive_result, "archive"

    try:
        live_result = await call(None)
    except Exception:
        if archive_result:
            log.warning(
                "read_session.archive_stale_served",
                extra={
                    "requested": session.isoformat(),
                    "answered": answered.isoformat() if answered else None,
                },
            )
            return archive_result, "archive_stale"
        raise
    return live_result, "live_fallback"
