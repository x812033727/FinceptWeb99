"""Daily fill-in for `discussions.daily_close_prices`.

Runs at 09:30 UTC = 17:30 Asia/Taipei — well after `ingest_ohlcv_tw`
(06:30 UTC) has populated the OHLCV archive, so the day-5 close for
yesterday's expiring discussions is available without a live
upstream call.

Scope:
  - Both auto-run and manual discussions are eligible (the user-
    facing "對答案" view promises coverage for everything that has
    a conclusion).
  - Only rows with NULL `daily_close_prices` get touched →
    idempotent, no-op on next tick after a row finishes resolving.
  - Skip discussions newer than 7 calendar days as a heuristic
    "5 trading days have surely elapsed" filter. Newer rows would
    just write a partial scoreboard the cron has to overwrite
    tomorrow anyway, so filtering keeps each tick cheap.

Failures on individual discussions are logged + counted; one bad
row never aborts the batch (mirrors the resilience pattern of
`tasks.auto_run_discussion`).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion
from services import discussion_scoreboard_service
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    record_failure,
    record_health,
)

log = logging.getLogger(__name__)

JOB_ID = "score_discussion_outcomes"

_LOCK_KEY = "lock:score_discussion_outcomes"
_LOCK_TTL = 10 * 60   # plenty of headroom for ~hundreds of pending rows
# Re-process recently created rows even when their daily_close_prices
# column is already populated — additional bars may have landed since
# the previous tick, so the saved scoreboard could be stale (e.g. only
# D1-D2 written yesterday, D3 available today). Older rows that were
# already fully resolved are stable and stay skipped.
_REFRESH_WINDOW_DAYS = 14


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("score_discussion_outcomes.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining)"
                ),
            )
            return

        try:
            scored, errors = await _do_run()
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "score_discussion_outcomes.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        if errors:
            await clear_failures(JOB_ID)
            await record_health(
                JOB_ID, ok=True, row_count=scored,
                error="; ".join(errors)[:500],
            )
        else:
            await clear_failures(JOB_ID)
            await record_health(JOB_ID, ok=True, row_count=scored)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> tuple[int, list[str]]:
    """Process every concluded discussion that needs (re)scoring.

    Two conditions trigger work:
      1. `daily_close_prices IS NULL` — the row has never been
         scored. Always process regardless of age.
      2. `created_at >= now - REFRESH_WINDOW_DAYS` — recently
         created rows may have new bars landing each day, so
         re-process to refresh partial windows. Old already-scored
         rows (>14 days) are stable; skip them.

    The `created_at <= now - 7 days` age cutoff used to live here
    as an optimisation (skip "still in progress" rows) but it had
    the side effect of preventing manual `Retry now` from filling
    in fresh discussions — partial windows were better than NULL.
    """
    refresh_floor = datetime.now(UTC) - timedelta(days=_REFRESH_WINDOW_DAYS)
    scored = 0
    errors: list[str] = []

    async with AsyncSessionLocal() as db:
        from sqlalchemy import or_
        stmt = (
            select(Discussion)
            .where(
                Discussion.conclusion.isnot(None),
                or_(
                    Discussion.daily_close_prices.is_(None),
                    Discussion.created_at >= refresh_floor,
                ),
            )
            .order_by(Discussion.created_at.asc())
        )
        rows = list((await db.scalars(stmt)).all())

        for row in rows:
            try:
                # Returns True iff every symbol got 5 days resolved —
                # but we persist regardless. Partial windows write
                # whatever they have so the UI shows partial data; the
                # next tick fills in additional days as bars land.
                fully = await discussion_scoreboard_service.persist_scoreboard(
                    db, row,
                )
                if fully or row.daily_close_prices:
                    scored += 1
            except Exception as exc:
                log.exception(
                    "score_discussion_outcomes.row_failed",
                    extra={"discussion_id": str(row.id)},
                )
                errors.append(f"{row.id}: {exc}")

    log.info(
        "score_discussion_outcomes.done",
        extra={"scored": scored, "errors": len(errors)},
    )
    return scored, errors
