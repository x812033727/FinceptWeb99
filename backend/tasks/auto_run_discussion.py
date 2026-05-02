"""Daily auto-run discussion — per-user opt-in.

Cron: 00:00 UTC = 08:00 Asia/Taipei (1h before TW market open at 09:00
Taipei). Skips weekends. Iterates every user who has flipped
`discussion_auto_run_configs.enabled` to true (PR #126) and runs one
discussion per user using their saved topic / rules / persona roster.
The resulting Discussion row is owned by the user themselves so it
shows up in their own DiscussionPage sidebar without any cross-user
permission changes.

Per-user idempotency: a second tick on the same UTC date sees the
existing auto_run row for that user and skips them. Health record's
`row_count` is the number of users we successfully ran for in this
tick (not the total enabled — failures and same-day duplicates don't
count).

Failure mode: if any user's run crashes, we log + record_failure +
move on to the next user. The current user's discussion row stays
with `status=draft` (run_round's finally-block atomic reset, PR #114),
`auto_run=true`, and no `verify_after_date` — so the verifier task
ignores it. The next-day idempotency check also ignores it (different
created_at date). Health is recorded with a per-user error summary.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from services import discussion_auto_run_config_service, discussion_service
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
# Each user's run is up to ~10 min; with N enabled users this can grow
# linearly. Lock TTL set generously so a slow run with many users isn't
# preempted mid-loop.
_LOCK_TTL = 60 * 60

_AUTO_ROUNDS = 5
_TW_SYMBOL_RE = re.compile(r"^\d{4,6}$")


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
            row_count, errors = await _do_run()
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

        if errors:
            # Partial success: some users ran, some failed. Report ok=true
            # so a single bad user doesn't trip auto-backoff for the
            # whole job, but surface the per-user errors in the health
            # row so admins notice.
            await clear_failures(JOB_ID)
            log.info(
                "auto_run_discussion.partial",
                extra={"rows_processed": row_count, "errors": errors},
            )
            await record_health(
                JOB_ID, ok=True, row_count=row_count,
                error="; ".join(errors)[:500],
            )
        else:
            await clear_failures(JOB_ID)
            log.info("auto_run_discussion.done", extra={"rows_processed": row_count})
            await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> tuple[int, list[str]]:
    """Drive one auto-run cycle across every enabled user.

    Returns (success_count, per_user_errors). `success_count` is the
    number of users we ran a fresh discussion for in this tick — same-
    day duplicates and failures don't bump it.
    """
    async with AsyncSessionLocal() as db:
        # Trading-day gate. Weekends always skip with health=ok and
        # row_count=0 — same as the legacy single-admin behaviour.
        if not await is_today_likely_trading_day(db):
            log.info("auto_run_discussion.skipped_not_trading_day")
            return 0, []

        configs = await discussion_auto_run_config_service.list_enabled(db)
        if not configs:
            log.info("auto_run_discussion.no_enabled_users")
            return 0, []

        successes = 0
        errors: list[str] = []
        for cfg in configs:
            try:
                ran = await _run_for_user(db, cfg)
                if ran:
                    successes += 1
            except Exception as exc:
                log.exception(
                    "auto_run_discussion.user_failed",
                    extra={"user_id": str(cfg.user_id), "error": str(exc)},
                )
                errors.append(f"user {cfg.user_id}: {exc}")
        return successes, errors


async def _run_for_user(
    db: AsyncSession, cfg: DiscussionAutoRunConfig,
) -> bool:
    """Run one auto-run discussion for a single enabled user.

    Returns True on a fresh successful run, False on a same-day skip.
    Raises on any other failure so the caller can record it and move on
    to the next user without aborting the whole tick.
    """
    user_id = cfg.user_id

    # Per-user idempotency: once per UTC date.
    today_utc = datetime.now(UTC).date()
    existing = await db.scalar(
        select(Discussion.id).where(
            Discussion.owner_id == user_id,
            Discussion.auto_run.is_(True),
            func.date(Discussion.created_at) == today_utc,
        ).limit(1)
    )
    if existing is not None:
        log.info(
            "auto_run_discussion.skipped_already_ran_today",
            extra={"user_id": str(user_id), "existing_id": str(existing)},
        )
        return False

    discussion = await discussion_service.create_discussion(
        db,
        owner_id=user_id,
        topic=cfg.topic,
        rules=cfg.rules,
        persona_ids=list(cfg.persona_ids or []),
        market=cfg.market,
    )
    # Flag this row as scheduler-produced so the verifier task picks
    # it up and the manual UI can render it differently if we ever
    # want to badge auto-run rows.
    await db.execute(
        update(Discussion)
        .where(Discussion.id == discussion.id)
        .values(auto_run=True)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    discussion.auto_run = True

    # Resolve the system-task LLM that all personas will use for this
    # auto-run. Admins set this in AdminPage → SystemTasksCard →
    # "auto_run_discussion_persona"; falls back to the SystemTaskSpec
    # compiled defaults if no DB override exists. Routing all personas
    # through one cheap model is the single cost knob for the system.
    from services.system_task_config_service import resolve as _resolve_task
    persona_provider, persona_model = await _resolve_task(
        db, "auto_run_discussion_persona",
    )
    log.info(
        "auto_run_discussion.persona_llm_resolved",
        extra={
            "user_id": str(user_id),
            "provider": persona_provider,
            "model": persona_model,
        },
    )

    for _round_idx in range(_AUTO_ROUNDS):
        async for _ev in discussion_service.run_round(
            db, discussion,
            user_id=str(user_id),
            provider_override=persona_provider,
            model_override=persona_model,
        ):
            pass

    conclusion = await discussion_service.synthesize_conclusion(
        db, discussion, user_id=str(user_id),
    )

    symbols = [
        str(s).strip()
        for s in (conclusion.get("recommended_symbols") or [])
        if _TW_SYMBOL_RE.fullmatch(str(s).strip())
    ]
    log.info(
        "auto_run_discussion.synthesized",
        extra={
            "user_id": str(user_id),
            "discussion_id": str(discussion.id),
            "symbols": symbols,
        },
    )

    verify_after = add_trading_days_estimate(utcnow_tw_date(), _AUTO_ROUNDS)
    await db.execute(
        update(Discussion)
        .where(Discussion.id == discussion.id)
        .values(verify_after_date=verify_after)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    discussion.verify_after_date = verify_after
    return True
