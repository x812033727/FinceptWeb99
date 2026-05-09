"""Daily snapshot cron for signal-audit history.

Once per UTC day, computes the bulk audit (last ~30 concluded
discussions per market) and persists per-signal counters into
``signal_audit_history``. The AdminPage SignalAuditCard reads this
table to render trend sparklines next to each coverage row.

Idempotent against the (captured_at, market, signal) PK — a re-run
on the same UTC day overwrites in place rather than appending
duplicates. Distributed lock via Redis keeps two pods from racing
the same write set (cheap operation, single connection, but
clarity beats free side effects).

One snapshot per market. Markets are taken from the discussion
archive, not hardcoded — if a new market shows up, its rows land
on the very next cron tick. The all-markets aggregate (`market
IS NULL`) is computed unconditionally.

Backoff is included for consistency with the other ingest tasks
even though pure-DB ops are unlikely to fail transiently — keeps
the admin UI's badge logic uniform.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
)
from services.signal_audit_service import snapshot_audit_to_history

log = logging.getLogger(__name__)

JOB_ID = "snapshot_signal_audit"

_LOCK_KEY = "lock:snapshot_signal_audit"
_LOCK_TTL = 5 * 60   # one cron tick of headroom — DB scan is fast


# Markets to snapshot per day. NULL = all-markets aggregate. The
# explicit list is a deliberate trade-off: we COULD enumerate
# distinct markets from the discussion table at runtime, but the
# stable list keeps the historical schema legible (admin can query
# `WHERE market='TW'` confidently knowing TW snapshots exist for
# every day).
_SNAPSHOT_SCOPES: tuple[str | None, ...] = (None, "TW", "US", "GLOBAL")


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("snapshot_signal_audit.skipped_lock_held")
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
            total_rows = 0
            async with AsyncSessionLocal() as db:
                for market in _SNAPSHOT_SCOPES:
                    try:
                        written = await snapshot_audit_to_history(
                            db, market=market,
                        )
                    except Exception as exc:
                        log.warning(
                            "snapshot_signal_audit.scope_failed",
                            extra={"market": market, "error": str(exc)},
                        )
                        continue
                    total_rows += written
                    log.info(
                        "snapshot_signal_audit.scope_done",
                        extra={"market": market, "rows": written},
                    )
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "snapshot_signal_audit.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"unexpected: {exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "snapshot_signal_audit.done",
            extra={"rows_processed": total_rows},
        )
        await record_health(JOB_ID, ok=True, row_count=total_rows)
    finally:
        await release_lock(_LOCK_KEY)
