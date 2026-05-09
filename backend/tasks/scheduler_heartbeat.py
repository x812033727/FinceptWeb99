"""APScheduler liveness heartbeat.

Writes a Redis key every interval so the AdminPage / health probes
can detect when APScheduler itself has died. Without this, a silent
scheduler crash (OOM, deadlock, event-loop wedge) would only surface
after the first daily-cadence cron's `last_run_at` aged out — a 24-30
hour blind window.

Design choices:

  - Interval: 30 s. Frequent enough that an outage shows in <1 minute,
    cheap enough that the Redis write cost is negligible (one SET per
    30 s = 2/min × 1440 min/day = 2880 writes/day — well below any
    sensible budget).

  - Redis TTL: 180 s. Lets the gauge tolerate a single missed tick
    (e.g. transient Redis blip, brief event-loop pause from a heavy
    discussion round) without false-firing. 6 missed ticks before the
    key disappears.

  - Stores ISO timestamp + a version stamp (taken from `_version.py`)
    so an admin can confirm WHICH process emitted the heartbeat — useful
    during a rolling deploy when two pods overlap.

  - NOT routed through `record_health` — heartbeat isn't an "ingest" in
    the IngestHealthCard sense; surfacing it as one would clutter the
    table with a 30s-cadence row. Has its own tiny health endpoint
    (`/admin/scheduler/health`) and its own Prometheus gauge
    (`scheduler_heartbeat_age_seconds`).

  - Failure path: if the Redis SET raises, we log a WARNING and move
    on. The AdminPage will pick up "stale" via TTL expiry on its own.
    Crashing the heartbeat task would leak the lock-free assumption —
    APScheduler's `max_instances=1 + coalesce=True` already guards
    against re-entry, but a permanent exception would mark the job
    failed and stop firing.
"""
import logging
from datetime import UTC, datetime

from cache.redis_cache import cache_set
from _version import __version__

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "scheduler:heartbeat"
HEARTBEAT_TTL_SECONDS = 180   # 6× the interval, tolerates 5 missed ticks


async def write_heartbeat() -> None:
    """Stamp the scheduler's liveness into Redis. Called every 30s by
    APScheduler. The reader (services.scheduler_health) computes
    `now - heartbeat.timestamp` to derive the freshness gauge.
    """
    payload = (
        f'{{"ts":"{datetime.now(UTC).isoformat()}",'
        f'"version":"{__version__}"}}'
    )
    try:
        await cache_set(HEARTBEAT_KEY, payload, HEARTBEAT_TTL_SECONDS)
    except Exception as exc:
        # Best-effort. The next 30s tick will retry; until it succeeds
        # the AdminPage will surface "scheduler heartbeat: stale" which
        # is the correct user-facing signal.
        log.warning(
            "scheduler.heartbeat.write_failed",
            extra={"error": str(exc)},
        )
