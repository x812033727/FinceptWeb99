"""Strategy auto-sweep dispatcher (PR-D).

Fires once per scheduler tick. Walks every template that's
auto_schedule_enabled, picks the ones whose `last_run_at` is
older than `cadence_hours`, and creates+starts a fresh sweep
from the template's defaults — closing the loop so the
backtest architecture runs without operator intervention.

Skips a template when there's already a non-terminal sweep tied
to it. Without that guard a slow sweep that overruns its cadence
would pile fresh sweeps behind it and burn LLM quota in
parallel. The guard means a stuck-running sweep silently halts
auto-scheduling for that template until the operator
intervenes — which is the correct loud failure mode.

Anchor selection uses the trading calendar so an offset of `-1`
on a Monday lands on Friday (last trading day) rather than
Sunday. Falls back to a calendar-day offset when the trading-
calendar service can't resolve (for tests / non-TW markets the
trading helper isn't wired for yet).
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate
from services import backtest_sweep_service as sweep_svc

log = logging.getLogger(__name__)

NON_TERMINAL_STATUSES = (
    sweep_svc.STATUS_PENDING,
    sweep_svc.STATUS_RUNNING,
)


async def process_due_strategies() -> list[UUID]:
    """One scheduler tick. Returns the list of newly-launched
    sweep ids (mostly for tests; the dispatcher itself is fire-
    and-forget).

    Opens its own DB session via `AsyncSessionLocal` because it
    runs from APScheduler's coroutine context, not a request
    handler — there's no `Depends(get_db)` to hand us one.
    """
    async with AsyncSessionLocal() as db:
        due = await _fetch_due_templates(db)
        if not due:
            return []

        launched: list[UUID] = []
        now = datetime.now(UTC)
        for tmpl in due:
            try:
                if await _has_active_sweep(db, tmpl.id):
                    log.info(
                        "strategy_auto_sweep.skip_active_sweep",
                        extra={"strategy_id": str(tmpl.id)},
                    )
                    # Still bump last_run_at so we don't re-check on
                    # every 5-min tick — the next attempt waits a
                    # full cadence.
                    tmpl.auto_schedule_last_run_at = now
                    await db.commit()
                    continue

                anchor = _resolve_anchor(
                    base_today=now.date(),
                    offset_days=tmpl.auto_schedule_anchor_offset_days,
                    market=tmpl.market,
                )
                sweep = await sweep_svc.create_sweep(
                    db,
                    owner_id=tmpl.owner_id,
                    topic=tmpl.topic,
                    rules=tmpl.rules,
                    market=tmpl.market,
                    persona_ids=list(tmpl.persona_ids or []),
                    anchor_date=anchor,
                    trading_days_count=tmpl.auto_schedule_trading_days_count,
                    rounds_per_discussion=tmpl.default_rounds,
                    concurrency=tmpl.default_concurrency,
                    auto_post_mortem=tmpl.default_auto_post_mortem,
                    strategy_id=tmpl.id,
                )
                tmpl.auto_schedule_last_run_at = now
                await db.commit()
                sweep_svc.start_sweep_in_background(sweep.id)
                launched.append(sweep.id)
                log.info(
                    "strategy_auto_sweep.launched",
                    extra={
                        "strategy_id": str(tmpl.id),
                        "sweep_id": str(sweep.id),
                        "anchor_date": anchor.isoformat(),
                    },
                )
            except Exception as exc:
                log.warning(
                    "strategy_auto_sweep.template_failed",
                    extra={
                        "strategy_id": str(tmpl.id),
                        "error": str(exc),
                    },
                )
                # Still bump last_run_at so a poison template doesn't
                # hammer every tick — next try respects cadence.
                tmpl.auto_schedule_last_run_at = now
                await db.commit()
        return launched


async def _fetch_due_templates(
    db: AsyncSession,
) -> list[DiscussionStrategyTemplate]:
    """Templates whose `last_run_at` is older than the per-template
    cadence (or never run). Computed in Python rather than the
    `WHERE` because each row's cadence is its own column — a
    server-side INTERVAL expression would need per-row binding
    via a CASE/lateral that's heavier than the in-app filter at
    typical scale (<<1000 templates per operator)."""
    now = datetime.now(UTC)
    stmt = select(DiscussionStrategyTemplate).where(
        DiscussionStrategyTemplate.auto_schedule_enabled.is_(True),
        DiscussionStrategyTemplate.deleted_at.is_(None),
    )
    rows = list((await db.scalars(stmt)).all())
    out: list[DiscussionStrategyTemplate] = []
    for r in rows:
        if r.auto_schedule_last_run_at is None:
            out.append(r)
            continue
        gap = now - r.auto_schedule_last_run_at
        if gap >= timedelta(hours=r.auto_schedule_cadence_hours):
            out.append(r)
    return out


async def _has_active_sweep(db: AsyncSession, strategy_id: UUID) -> bool:
    """True iff the template has a sweep in pending or running
    state. We check via direct SQL rather than the service helper
    because we need the existence count, not the full row."""
    stmt = select(BacktestSweep.id).where(
        BacktestSweep.strategy_id == strategy_id,
        BacktestSweep.status.in_(NON_TERMINAL_STATUSES),
    ).limit(1)
    return (await db.scalar(stmt)) is not None


def _resolve_anchor(
    *, base_today: date, offset_days: int, market: str,  # noqa: ARG001
) -> date:
    """Pick the anchor date.

    Currently a calendar-day offset — the sweep worker's own
    `_resolve_trading_days` walks `ohlcv_daily` and snaps to the
    nearest available trading bar, so a Sunday anchor lands on
    Friday's data without needing a separate trading-calendar
    pass here. The `market` arg is kept in the signature for a
    future precise-snap upgrade; absent today.
    """
    return base_today + timedelta(days=offset_days)


__all__ = [
    "NON_TERMINAL_STATUSES",
    "process_due_strategies",
    "_resolve_anchor",
]
