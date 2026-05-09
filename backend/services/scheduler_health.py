"""Read-side helpers for the APScheduler liveness heartbeat.

The heartbeat write happens in `tasks/scheduler_heartbeat.py`. This
module reads it back: parses the JSON payload, computes the freshness
delta against `now()`, and updates the Prometheus gauge. Used by:

  - `GET /api/admin/scheduler/health` for the AdminPage display
  - operators scraping `/metrics` for alerting / dashboards
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from cache.redis_cache import cache_get
from tasks.scheduler_heartbeat import HEARTBEAT_KEY, HEARTBEAT_TTL_SECONDS

log = logging.getLogger(__name__)


# Heartbeat is "stale" when older than 2× its interval (60s). A single
# missed tick (transient Redis blip, GC pause) shouldn't trip the
# warning — but a full minute without a tick almost certainly means
# the scheduler is wedged.
STALE_THRESHOLD_SECONDS = 60


@dataclass
class SchedulerHeartbeat:
    """One-shot snapshot of the scheduler's liveness signal.

    `age_seconds` is the freshness delta the AdminPage renders. The
    Pydantic schema in `api/admin/schemas.py` mirrors this dataclass
    one-to-one — keep them in sync.
    """

    last_beat_at: str | None
    age_seconds: float | None
    stale: bool
    version: str | None
    ttl_seconds: int


async def read_heartbeat() -> SchedulerHeartbeat:
    """Resolve the current scheduler heartbeat snapshot.

    Returns:
      - `last_beat_at=None` + `age_seconds=None` + `stale=True` when
        the key is missing (scheduler has been dead longer than the
        TTL window, or has never written a tick yet).
      - All fields populated when the key exists. `stale=True` when
        `age_seconds > STALE_THRESHOLD_SECONDS`.

    Falls open on Redis outage (returns the missing-key shape) so the
    health endpoint stays available even when Redis is down — the UI
    will surface "scheduler heartbeat: missing" which is at least as
    informative as a 500 error.
    """
    try:
        raw = await cache_get(HEARTBEAT_KEY)
    except Exception as exc:
        log.warning(
            "scheduler.heartbeat.read_failed",
            extra={"error": str(exc)},
        )
        raw = None

    if not raw:
        _set_gauge(None)
        return SchedulerHeartbeat(
            last_beat_at=None,
            age_seconds=None,
            stale=True,
            version=None,
            ttl_seconds=HEARTBEAT_TTL_SECONDS,
        )

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        _set_gauge(None)
        return SchedulerHeartbeat(
            last_beat_at=None,
            age_seconds=None,
            stale=True,
            version=None,
            ttl_seconds=HEARTBEAT_TTL_SECONDS,
        )

    ts_raw = data.get("ts")
    version = data.get("version")
    age: float | None = None
    if isinstance(ts_raw, str):
        try:
            beat_at = datetime.fromisoformat(ts_raw)
            if beat_at.tzinfo is None:
                beat_at = beat_at.replace(tzinfo=UTC)
            age = max(0.0, (datetime.now(UTC) - beat_at).total_seconds())
        except ValueError:
            age = None

    _set_gauge(age)
    return SchedulerHeartbeat(
        last_beat_at=ts_raw if isinstance(ts_raw, str) else None,
        age_seconds=age,
        stale=(age is None or age > STALE_THRESHOLD_SECONDS),
        version=version if isinstance(version, str) else None,
        ttl_seconds=HEARTBEAT_TTL_SECONDS,
    )


def _set_gauge(age: float | None) -> None:
    """Push the freshness delta into the Prometheus gauge.

    `-1` when the heartbeat key is missing — distinguishes "scheduler
    has never beat" from "scheduler beat 0 seconds ago" so an alerting
    rule can match `scheduler_heartbeat_age_seconds == -1` for the
    fully-dead case.
    """
    try:
        from middleware.metrics import SCHEDULER_HEARTBEAT_AGE_SECONDS

        SCHEDULER_HEARTBEAT_AGE_SECONDS.set(age if age is not None else -1.0)
    except Exception as exc:
        log.debug(
            "scheduler.heartbeat.gauge_failed",
            extra={"error": str(exc)},
        )
