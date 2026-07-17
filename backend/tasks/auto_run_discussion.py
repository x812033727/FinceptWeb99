"""Daily auto-run discussion — per-user opt-in.

Cron: 20:00 UTC = 04:00 Asia/Taipei next day (5h before TW market
open at 09:00 Taipei). Skips weekends. Iterates every user who has
flipped `discussion_auto_run_configs.enabled` to true (PR #126) and
runs one discussion per user using their saved topic / rules /
persona roster. The resulting Discussion row is owned by the user
themselves so it shows up in their own DiscussionPage sidebar without
any cross-user permission changes.

No same-day skip: every tick runs a fresh discussion for each enabled
user, even when one already exists for the current Taipei day. The
previous per-user idempotency guard (keyed on the Taipei calendar day)
was removed so that a half-finished draft left behind by an earlier
failed run — or any second invocation — can no longer block the user
from getting their daily discussion. Health record's `row_count` is the
number of users we successfully ran for in this tick (not the total
enabled — failures still don't count).

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
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion, DiscussionTurn
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.user import User
from services import discussion_auto_run_config_service, discussion_service, email_service
from services.daily_stock_strategies import (
    build_topic,
    candidate_batches,
    candidate_pool,
    rank_candidates,
)
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    record_failure,
    record_health,
)
from services.notification_service import notify_user
from services.tw_trading_calendar import is_today_likely_trading_day

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
                JOB_ID,
                ok=False,
                row_count=0,
                error=(f"skipped (backoff after {failures} failures, " f"~{mins} min remaining)"),
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
                JOB_ID,
                ok=False,
                row_count=0,
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
                JOB_ID,
                ok=True,
                row_count=row_count,
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
    number of users we ran a fresh discussion for in this tick — only
    failures don't bump it (there is no same-day skip anymore).
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
    db: AsyncSession,
    cfg: DiscussionAutoRunConfig,
) -> bool:
    """Run one auto-run discussion for a single enabled user.

    Always returns True on a successful run — there is no same-day skip
    anymore (a fresh discussion is created on every tick regardless of
    whether the user already has one for the current Taipei day). Raises
    on any failure so the caller can record it and move on to the next
    user without aborting the whole tick.
    """
    user_id = cfg.user_id
    counts = discussion_auto_run_config_service.normalize_strategy_run_counts(
        getattr(cfg, "strategy_run_counts", None),
        legacy_enabled=True,
    )
    run_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
    candidate_rows = await load_candidate_rows(db)
    ran_strategies: dict[str, int] = {}
    for strategy, count in counts.items():
        if count <= 0:
            continue
        ranked = rank_candidates(strategy, candidate_rows)
        batches = candidate_batches(ranked, count)
        # The full (capped, slim) pool is a per-day per-strategy fact, so
        # it rides on the sequence-1 snapshot only.
        pool = candidate_pool(ranked)
        # Backward-compatible general discussion when the deployment's market
        # snapshot provider has not produced a universe yet.
        if strategy == "general" and not batches:
            batches = [[]]
        for sequence, batch in enumerate(batches, 1):
            exists = await db.scalar(
                select(Discussion.id).where(
                    Discussion.owner_id == user_id,
                    Discussion.auto_run_date == run_date,
                    Discussion.auto_run_strategy == strategy,
                    Discussion.auto_run_sequence == sequence,
                )
            )
            if exists:
                continue
            await _run_strategy_slot(
                db, cfg, strategy, sequence, run_date, batch,
                pool=pool if sequence == 1 else None,
            )
            ran_strategies[strategy] = ran_strategies.get(strategy, 0) + 1
    if ran_strategies:
        await _notify_daily_ready(str(user_id), run_date, ran_strategies)
    return bool(ran_strategies)


async def _notify_daily_ready(
    user_id: str, run_date, ran_strategies: dict[str, int],
) -> None:
    """Best-effort 'your daily picks are ready' fan-out over the
    existing notification transports (websocket / web push / email /
    LINE, each per user opt-in). Must never fail the run itself."""
    slots = sum(ran_strategies.values())
    try:
        await notify_user(user_id, {
            "kind": "daily_picks_ready",
            "type": "daily_picks",
            "title": "今日 AI 選股完成",
            "message": (
                f"{run_date.isoformat()} 已完成 {len(ran_strategies)} 個策略"
                f"共 {slots} 場討論，歡迎查看每日精選。"
            ),
            "run_date": run_date.isoformat(),
            "strategies": sorted(ran_strategies),
            "discussions": slots,
        })
    except Exception:
        log.exception(
            "auto_run_discussion.notify_failed", extra={"user_id": user_id},
        )


async def load_candidate_rows(db: AsyncSession) -> list[dict]:
    """Build one settled-data snapshot from the local TW archives."""
    from collections import defaultdict

    from models.fundamentals_snapshot import FundamentalsSnapshot
    from models.ohlcv_daily import OhlcvDaily
    from models.tw_chip_metrics import TwInstitutionalDaily
    from models.tw_revenue_monthly import TwRevenueMonthly

    bars = (
        await db.scalars(
            select(OhlcvDaily)
            .where(OhlcvDaily.market == "TW")
            .order_by(OhlcvDaily.symbol, OhlcvDaily.ts.desc())
        )
    ).all()
    by_symbol: dict[str, list] = defaultdict(list)
    for bar in bars:
        if len(by_symbol[bar.symbol]) < 40:
            by_symbol[bar.symbol].append(bar)

    def latest_by_symbol(items):
        result = {}
        for item in items:
            if item.symbol not in result:
                result[item.symbol] = item
        return result

    fundamentals = latest_by_symbol(
        (
            await db.scalars(
                select(FundamentalsSnapshot)
                .where(FundamentalsSnapshot.market == "TW")
                .order_by(FundamentalsSnapshot.as_of.desc())
            )
        ).all()
    )
    revenues = latest_by_symbol(
        (
            await db.scalars(
                select(TwRevenueMonthly)
                .where(TwRevenueMonthly.market == "TW")
                .order_by(TwRevenueMonthly.ts.desc())
            )
        ).all()
    )
    chip_rows = (
        await db.scalars(
            select(TwInstitutionalDaily)
            .where(TwInstitutionalDaily.market == "TW")
            .order_by(TwInstitutionalDaily.symbol, TwInstitutionalDaily.ts.desc())
        )
    ).all()
    chips: dict[str, list] = defaultdict(list)
    for item in chip_rows:
        if len(chips[item.symbol]) < 5:
            chips[item.symbol].append(item)

    snapshots = []
    for symbol, newest_first in by_symbol.items():
        # No-trade sessions (suspended symbols, cash-settled beneficiary
        # certificates) arrive from TWSE as NULL-priced rows. Drop them up
        # front: every window below indexes `series` positionally and reads
        # `.close` unguarded, so a sparse series either raises or silently
        # measures non-adjacent sessions against each other.
        series = [x for x in reversed(newest_first) if x.close is not None]
        if len(series) < 21:
            continue
        closes = [float(x.close) for x in series]
        last = series[-1]
        volumes = [int(x.volume or 0) for x in series[-21:-1]]
        gains = [closes[i] - closes[i - 1] for i in range(max(1, len(closes) - 14), len(closes))]
        avg_gain = sum(max(x, 0) for x in gains) / 14
        avg_loss = sum(max(-x, 0) for x in gains) / 14
        rsi = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        institutionals = chips.get(symbol, [])
        nets = [int(x.fini_buy or 0) - int(x.fini_sell or 0) for x in institutionals]
        fund = fundamentals.get(symbol)
        payload = (fund.payload or {}) if fund else {}
        revenue = revenues.get(symbol)
        snapshots.append(
            {
                "symbol": symbol,
                "close": float(last.close),
                "history_days": len(series),
                "avg_volume_20d": sum(volumes) / len(volumes),
                "volume_ratio": int(last.volume or 0) / max(1, sum(volumes) / len(volumes)),
                "prior_high_20d": max(float(x.high or x.close) for x in series[-21:-1]),
                "return_1d": closes[-1] / closes[-2] - 1,
                "return_5d": closes[-1] / closes[-6] - 1,
                "return_20d": closes[-1] / closes[-21] - 1,
                "rsi": rsi,
                "stabilizing": closes[-1] >= closes[-2],
                "foreign_buy_days_5d": sum(n > 0 for n in nets),
                "foreign_net_buy_5d": sum(nets),
                "foreign_net_buy_1d": nets[0] if nets else 0,
                "pe": float(fund.pe_ratio) if fund and fund.pe_ratio is not None else None,
                "revenue_yoy": float(revenue.revenue_yoy)
                if revenue and revenue.revenue_yoy is not None
                else None,
                "roe": payload.get("roe"),
                "operating_cash_flow": payload.get("operating_cash_flow"),
                "ocf_positive_quarters": payload.get("ocf_positive_quarters", 0),
                "debt_ratio": payload.get("debt_ratio", 100),
            }
        )
    return snapshots


async def _run_strategy_slot(
    db, cfg, strategy, sequence, run_date, batch, pool: list | None = None
) -> None:
    user_id = cfg.user_id

    # Live mode (no `as_of_date`): this is a forward-looking daily call,
    # not a backtest — so it must NOT carry the backtest anchor (which
    # flips the UI to 「回測」, routes the whole context through the
    # clamped `ohlcv_daily` replay path, and gates it as a backtest row).
    # The cron fires at 20:00 UTC = 04:00 Taipei *next* day (pre-market),
    # but determinism of the settled close is now guaranteed by
    # `tw_market_service.get_quote`'s closed-market `ohlcv_daily`
    # self-heal (mirrors the screener fast-path) — so personas read the
    # prior session's settled close without the pre-market TWSE feed's
    # publication-lag staleness, and without pretending to be a backtest.
    topic = cfg.topic if not batch else build_topic(strategy, batch, cfg.topic)
    discussion = await discussion_service.create_discussion(
        db,
        owner_id=user_id,
        topic=topic,
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
        .values(
            auto_run=True,
            auto_run_strategy=strategy,
            auto_run_sequence=sequence,
            auto_run_date=run_date,
            candidate_snapshot={
                "strategy": strategy,
                "sequence": sequence,
                "candidates": batch,
                **({"pool": pool} if pool is not None else {}),
            },
        )
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
        db,
        "auto_run_discussion_persona",
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
            db,
            discussion,
            user_id=str(user_id),
            provider_override=persona_provider,
            model_override=persona_model,
        ):
            pass

    conclusion = await discussion_service.synthesize_conclusion(
        db,
        discussion,
        user_id=str(user_id),
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
    return None


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

    turns = list(
        (
            await db.scalars(
                select(DiscussionTurn)
                .where(DiscussionTurn.discussion_id == discussion.id)
                .order_by(DiscussionTurn.round, DiscussionTurn.turn_index)
            )
        ).all()
    )

    from ai.agents import get_agent

    persona_name: dict[str, str] = {}
    for pid in discussion.persona_ids or []:
        try:
            persona_name[pid] = get_agent(pid).name
        except ValueError:
            persona_name[pid] = pid

    body = email_service.render_discussion_report_markdown(
        discussion,
        conclusion,
        turns,
        persona_name=persona_name,
    )
    created_tw = discussion.created_at.strftime("%Y-%m-%d") if discussion.created_at else ""
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
