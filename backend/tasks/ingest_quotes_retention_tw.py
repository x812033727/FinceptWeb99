"""Daily prune of quote_snapshots beyond the retention window.

The TW refresh task writes one row per active symbol per minute during
market hours; without retention, the table grows ~3M rows / yr at full
load. 30-day retention is the v1 default — operators who want longer
windows can lift the constant or turn on TimescaleDB compression.

Multi-pod safe via Redis SET-NX lock. Backoff is included for
consistency with the other ingest tasks even though pure-DB ops are
unlikely to fail transiently — keeps the admin UI's badge logic uniform.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    prune_quote_snapshots,
    record_failure,
    record_health,
)

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
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            previous = await get_health(JOB_ID)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                tail = f"; last: {previous.error[:200]}"
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining{tail})"
                ),
            )
            return

        try:
            async with AsyncSessionLocal() as db:
                deleted = await prune_quote_snapshots(
                    db, older_than_days=RETENTION_DAYS,
                )
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_quotes_retention_tw.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"unexpected: {exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_quotes_retention_tw.done",
            extra={"rows_processed": deleted},
        )
        await record_health(JOB_ID, ok=True, row_count=deleted)
    finally:
        await release_lock(_LOCK_KEY)
