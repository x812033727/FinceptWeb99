"""Daily auto-run discussion — per-user opt-in.

Cron: 20:00 UTC = 04:00 Asia/Taipei next day (5h before TW market
open at 09:00 Taipei). Skips weekends. Iterates every user who has
flipped `discussion_auto_run_configs.enabled` to true (PR #126) and
runs one discussion per user using their saved topic / rules /
persona roster. The resulting Discussion row is owned by the user
themselves so it shows up in their own DiscussionPage sidebar without
any cross-user permission changes.

Per-user idempotency: keyed on the Taipei calendar day (not the UTC
date) so 20:00 UTC Sunday and 03:59 UTC Monday — both Monday in
Taipei — count as the same tick. The filter uses a half-open UTC
range covering Taipei 00:00→24:00 on the resolved Taipei date.
Health record's `row_count` is the number of users we successfully
ran for in this tick (not the total enabled — failures and same-day
duplicates don't count).

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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion, DiscussionTurn
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.user import User
from services import discussion_auto_run_config_service, discussion_service, email_service
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    record_failure,
    record_health,
)
from services.tw_trading_calendar import (
    is_today_likely_trading_day,
    prev_trading_day_estimate,
    tw_day_utc_bounds,
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
    """Entry point invoked by APScheduler at 20:00 UTC daily (= 04:00
    Asia/Taipei next day)."""
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

    # Per-user idempotency: once per Taipei calendar day. Filter via a
    # half-open UTC range covering 00:00→24:00 Taipei on the resolved
    # Taipei date — `created_at` is timestamptz, so a plain
    # `func.date(...)` extracts the UTC date and would be off-by-one
    # against a Taipei-localised today.
    today_tw = utcnow_tw_date()
    tw_start, tw_end = tw_day_utc_bounds(today_tw)
    existing = await db.scalar(
        select(Discussion.id).where(
            Discussion.owner_id == user_id,
            Discussion.auto_run.is_(True),
            Discussion.created_at >= tw_start,
            Discussion.created_at < tw_end,
        ).limit(1)
    )
    if existing is not None:
        log.info(
            "auto_run_discussion.skipped_already_ran_today",
            extra={"user_id": str(user_id), "existing_id": str(existing)},
        )
        return False

    # Anchor the run to the last completed TW trading day (Taipei-local).
    # The cron fires at 20:00 UTC = 04:00 Taipei *next* day — pre-market,
    # so the freshest settled session is the prior trading day. Passing
    # `as_of_date` routes context gathering through the deterministic
    # `ohlcv_daily` read path (clamped `ts <= as_of`) instead of the live
    # pre-market TWSE feed, whose recency depends on end-of-day
    # publication lag. Without it the personas saw stale quotes.
    anchor = prev_trading_day_estimate(utcnow_tw_date())
    discussion = await discussion_service.create_discussion(
        db,
        owner_id=user_id,
        topic=cfg.topic,
        rules=cfg.rules,
        persona_ids=list(cfg.persona_ids or []),
        market=cfg.market,
        as_of_date=anchor,
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

    if cfg.send_email:
        await _maybe_send_report_email(db, cfg, discussion, conclusion)

    # `verify_after_date` is now seeded inside `synthesize_conclusion`
    # (PR #218), so the dedicated UPDATE that used to live here is
    # redundant and was using `_AUTO_ROUNDS=5` as a calendar-day arg
    # to `add_trading_days_estimate` (semantically wrong even if
    # numerically correct). Trust the service-layer set; if a test
    # explicitly bypasses it via direct DB writes, that test owns
    # setting verify_after_date too.
    return True


async def _maybe_send_report_email(
    db: AsyncSession,
    cfg: DiscussionAutoRunConfig,
    discussion: Discussion,
    conclusion: dict,
) -> None:
    """Send the post-run discussion report to the opted-in user's
    account email. Fail-closed on every error — the auto-run task's
    primary deliverable (the discussion row) has already landed, so
    an email transport failure must not propagate out and trip
    auto-backoff.

    Skip silently (just a log warning) when:
    - SMTP isn't configured on this deployment (`is_configured()`)
    - the user's row is missing (shouldn't happen — FK to users)
    - the user has no email on file
    """
    if not email_service.is_configured():
        log.warning(
            "auto_run_discussion.email_skipped_not_configured",
            extra={"user_id": str(cfg.user_id)},
        )
        return

    user = await db.scalar(select(User).where(User.id == cfg.user_id))
    if user is None or not (user.email or "").strip():
        log.warning(
            "auto_run_discussion.email_skipped_no_address",
            extra={"user_id": str(cfg.user_id)},
        )
        return

    turns = list((await db.scalars(
        select(DiscussionTurn)
        .where(DiscussionTurn.discussion_id == discussion.id)
        .order_by(DiscussionTurn.round, DiscussionTurn.turn_index)
    )).all())

    from ai.agents import get_agent
    persona_name: dict[str, str] = {}
    for pid in (discussion.persona_ids or []):
        try:
            persona_name[pid] = get_agent(pid).name
        except ValueError:
            persona_name[pid] = pid

    body = email_service.render_discussion_report_markdown(
        discussion, conclusion, turns, persona_name=persona_name,
    )
    created_tw = (
        discussion.created_at.strftime("%Y-%m-%d")
        if discussion.created_at else ""
    )
    subject = f"[Fincept] 每日專家圓桌討論 {created_tw}".strip()

    try:
        await email_service.send_email(
            to=user.email,
            subject=subject,
            body_markdown=body,
            attachment_filename=f"discussion_{discussion.id}.md",
            attachment_content=body,
        )
        log.info(
            "auto_run_discussion.email_sent",
            extra={
                "user_id": str(cfg.user_id),
                "discussion_id": str(discussion.id),
                "to": user.email,
            },
        )
    except Exception as exc:
        log.warning(
            "auto_run_discussion.email_failed",
            extra={
                "user_id": str(cfg.user_id),
                "discussion_id": str(discussion.id),
                "error": str(exc),
            },
        )
