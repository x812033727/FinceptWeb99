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
`ingest_quotes_retention_tw`.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.discussion_service import prune_old_round_contexts
from services.ingest.repository import record_health

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
        async with AsyncSessionLocal() as db:
            deleted = await prune_old_round_contexts(
                db, older_than_days=RETENTION_DAYS,
            )
        log.info(
            "prune_discussion_contexts.done", extra={"deleted": deleted},
        )
        await record_health(JOB_ID, ok=True, row_count=deleted)
    except Exception as exc:
        log.exception("prune_discussion_contexts.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)
