"""Freshness rules for host-managed ingest schedules."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

FINMIND_TW_MARKETWIDE_JOB_ID = "finmind_tw_marketwide"
FINMIND_TW_MARKETWIDE_GRACE = timedelta(minutes=45)
FINMIND_TW_MARKETWIDE_SLOTS = (
    time(15, 10),
    time(17, 30),
    time(22, 0),
)
_TAIPEI = ZoneInfo("Asia/Taipei")


def latest_required_finmind_tw_slot(*, now: datetime | None = None) -> datetime:
    """Return the latest weekday slot whose 45-minute grace has elapsed.

    The result is timezone-aware in Asia/Taipei. Before Monday's first due
    slot and throughout weekends, the previous Friday 22:00 slot remains the
    latest requirement.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current.astimezone(_TAIPEI) - FINMIND_TW_MARKETWIDE_GRACE
    candidate_day = cutoff.date()

    while True:
        if candidate_day.weekday() < 5:
            for slot in reversed(FINMIND_TW_MARKETWIDE_SLOTS):
                candidate = datetime.combine(
                    candidate_day,
                    slot,
                    tzinfo=_TAIPEI,
                )
                if candidate <= cutoff:
                    return candidate
        candidate_day -= timedelta(days=1)


def is_finmind_tw_marketwide_run_stale(
    last_run_at: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether the fixed host cron missed its latest required run slot."""
    if not last_run_at:
        return True
    try:
        parsed = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(_TAIPEI) < latest_required_finmind_tw_slot(now=now)
