"""Daily prune of `discussion_round_contexts` beyond the retention window.

Each discussion round writes a `gather_market_context` JSON snapshot
(~30-50 KB; can hit 100 KB+ when focus_briefs has 5 symbols). With no
GC the table grows linearly forever — a user with 100 discussions ×
5 rounds = 500 snapshots burns ~25 MB on its own; the deployment-
wide footprint compounds as more users sign up.

90-day retention matches `_PRIOR_DISCUSSIONS_LOOKBACK_DAYS` so the
replay archive ages out together with the cross-session memory
window. Discussions + `discussion_turns` rows are NOT touched —
those are tiny and worth keeping forever for the public scoreboard
+ self-grading verifier.

Multi-pod safe via Redis SET-NX lock; mirrors
`ingest_quotes_retention_tw`. Backoff included for consistency with
the other ingest tasks.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.discussion_service import prune_old_round_contexts
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
)

log = logging.getLogger(__name__)

JOB_ID = "prune_discussion_contexts"
RETENTION_DAYS = 90

_LOCK_KEY = "lock:prune_discussion_contexts"
_LOCK_TTL = 30 * 60


async def run() -> None:
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("prune_discussion_contexts.skipped_lock_held")
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
                deleted = await prune_old_round_contexts(
                    db, older_than_days=RETENTION_DAYS,
                )
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "prune_discussion_contexts.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"unexpected: {exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "prune_discussion_contexts.done",
            extra={"rows_processed": deleted},
        )
        await record_health(JOB_ID, ok=True, row_count=deleted)
    finally:
        await release_lock(_LOCK_KEY)
