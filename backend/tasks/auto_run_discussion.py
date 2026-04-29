"""Daily auto-run discussion — 5 rounds + synthesizer + verdict prep.

Cron: 00:00 UTC = 08:00 Asia/Taipei (1h before TW market open at 09:00
Taipei). Skips weekends + when ADMIN_EMAIL isn't configured. Idempotent:
a second tick on the same UTC date sees the existing row and bails.

Why this lives in its own task and not inside the discussion router:
the work runs untethered to any HTTP request (no SSE consumer, no
streaming back to a client), so reusing the existing route would just
mean spinning up an unnecessary StreamingResponse. The route also
spawns its own per-request `asyncio.create_task` to survive client
disconnect (PR #118) — that machinery is wasted overhead when the
"client" is the scheduler itself.

Failure mode: if any round crashes, the discussion row stays with
`status=draft` (run_round's finally-block atomic reset, PR #114),
`auto_run=true`, and no `verify_after_date` — so the verifier task
ignores it. The next-day idempotency check also ignores it (different
created_at date). Health is recorded, ops can investigate.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import func, select, update

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion
from services import discussion_service
from services.admin_user_service import get_admin_owner_id
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    record_failure,
    record_health,
)
from services.tw_trading_calendar import (
    add_trading_days_estimate,
    is_today_likely_trading_day,
    utcnow_tw_date,
)

log = logging.getLogger(__name__)

JOB_ID = "auto_run_discussion"
_LOCK_KEY = "lock:auto_run_discussion"
# 5 rounds × 4 personas × ~30 s/turn ≈ 10 min on a healthy LLM. Lock
# TTL is generous so a slow-but-progressing run isn't preempted.
_LOCK_TTL = 30 * 60

_AUTO_ROUNDS = 5
_TW_SYMBOL_RE = re.compile(r"^\d{4,6}$")

# Mirrors the frontend defaults at frontend/src/pages/DiscussionPage.tsx
# (DEFAULT_TOPIC / DEFAULT_RULES / DEFAULT_PERSONAS). Kept inline here
# so a frontend localStorage edit doesn't silently change the system
# task's behaviour. Edit both sites together when tuning.
_AUTO_TOPIC = (
    "找出本週（未來 5 個交易日）值得短線進場的台股 1-3 檔，"
    "並列出進場條件與停損點。"
)
_AUTO_RULES = "\n".join([
    "1. 每位專家發言 ≤ 200 字。",
    "2. 必須引用至少一個具體數據（價、量、法人、新聞情緒）。",
    "3. 反對其他專家時必須點名是反對誰、為什麼。",
    "4. 不得推薦未在「市場現況」的 top_gainers / top_losers 中出現的標的。",
    "5. 短線定義：5 個交易日內出場。",
])
_AUTO_PERSONAS = ["market_analyst", "trading_coach", "lynch", "simons"]


async def run() -> None:
    """Entry point invoked by APScheduler at 00:00 UTC daily."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("auto_run_discussion.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            log.info(
                "auto_run_discussion.skipped_backoff",
                extra={"failures": failures, "seconds_remaining": remaining},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining)"
                ),
            )
            return

        try:
            row_count = await _do_run()
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "auto_run_discussion.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info("auto_run_discussion.done", extra={"rows_processed": row_count})
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    """Drive one auto-run cycle. Returns 1 on success, 0 on legitimate
    skip (weekend / already-ran-today)."""
    async with AsyncSessionLocal() as db:
        # Trading-day gate. Weekends always skip with health=ok.
        if not await is_today_likely_trading_day(db):
            log.info("auto_run_discussion.skipped_not_trading_day")
            return 0

        # Idempotency: once per UTC date.
        today_utc = datetime.now(UTC).date()
        existing = await db.scalar(
            select(Discussion.id).where(
                Discussion.auto_run.is_(True),
                func.date(Discussion.created_at) == today_utc,
            ).limit(1)
        )
        if existing is not None:
            log.info(
                "auto_run_discussion.skipped_already_ran_today",
                extra={"existing_id": str(existing)},
            )
            return 0

        # Owner — admin user. Failure here is a config error worth
        # surfacing to ops, so we raise into the outer health-record
        # path rather than silently skipping.
        owner_id = await get_admin_owner_id(db)
        if owner_id is None:
            raise RuntimeError(
                "ADMIN_EMAIL not set or no matching user — set ADMIN_EMAIL "
                "in env / DB to a real user to enable auto-run discussions"
            )

        discussion = await discussion_service.create_discussion(
            db,
            owner_id=owner_id,
            topic=_AUTO_TOPIC,
            rules=_AUTO_RULES,
            persona_ids=_AUTO_PERSONAS,
        )
        # Flag this row as scheduler-produced so the verifier task
        # picks it up (and so the manual UI can render it differently
        # if we ever want to badge auto-run rows).
        await db.execute(
            update(Discussion)
            .where(Discussion.id == discussion.id)
            .values(auto_run=True)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        discussion.auto_run = True

        # Run N rounds sequentially. Each call is an async generator
        # that yields SSE events; we drain them — persistence happens
        # as side-effects inside run_round.
        for _round_idx in range(_AUTO_ROUNDS):
            async for _ev in discussion_service.run_round(
                db, discussion, user_id=str(owner_id),
            ):
                pass

        conclusion = await discussion_service.synthesize_conclusion(
            db, discussion, user_id=str(owner_id),
        )

        # Filter to plausibly-TW codes so the verifier doesn't try to
        # look up "AAPL" against the TW connector. Empty result is
        # fine — verifier handles `unverifiable` for the no-symbol case.
        symbols = [
            str(s).strip()
            for s in (conclusion.get("recommended_symbols") or [])
            if _TW_SYMBOL_RE.fullmatch(str(s).strip())
        ]
        log.info(
            "auto_run_discussion.synthesized",
            extra={
                "discussion_id": str(discussion.id),
                "symbols": symbols,
            },
        )

        # `verify_after_date` is the first calendar date the verifier
        # may grade this row. Best-effort estimate (5 weekdays); the
        # verifier itself re-checks against actual OHLCV bars and
        # defers if the window's missing data due to a holiday.
        verify_after = add_trading_days_estimate(utcnow_tw_date(), _AUTO_ROUNDS)
        await db.execute(
            update(Discussion)
            .where(Discussion.id == discussion.id)
            .values(verify_after_date=verify_after)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        # Mirror onto the in-memory instance so any caller holding the
        # row (tests, callers passing it onward) sees the new value
        # without an explicit refetch — same pattern as PR #114 for
        # status reset.
        discussion.verify_after_date = verify_after
        return 1
