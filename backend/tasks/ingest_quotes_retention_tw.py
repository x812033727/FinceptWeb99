"""Daily prune of quote_snapshots beyond the retention window.

The TW refresh task writes one row per active symbol per minute during
market hours; without retention, the table grows ~3M rows / yr at full
load. 30-day retention is the v1 default — operators who want longer
windows can lift the constant or turn on TimescaleDB compression.

Multi-pod safe via Redis SET-NX lock.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import prune_quote_snapshots, record_health

log = logging.getLogger(__name__)

JOB_ID = "ingest_quotes_retention_tw"
RETENTION_DAYS = 30

_LOCK_KEY = "lock:ingest_quotes_retention_tw"
_LOCK_TTL = 30 * 60


async def run() -> None:
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_quotes_retention_tw.skipped_lock_held")
        return
    try:
        async with AsyncSessionLocal() as db:
            deleted = await prune_quote_snapshots(db, older_than_days=RETENTION_DAYS)
        log.info("ingest_quotes_retention_tw.done", extra={"deleted": deleted})
        await record_health(JOB_ID, ok=True, row_count=deleted)
    except Exception as exc:
        log.exception("ingest_quotes_retention_tw.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)
