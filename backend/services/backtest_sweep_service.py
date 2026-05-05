"""Automated multi-day backtest sweep runner (PR #274).

Operator picks an anchor date + a trading-day count + rounds-per-
discussion. The sweep worker resolves the actual N trading days
from `ohlcv_daily`, then for each date:

  1. Creates a new Discussion with `as_of_date=that_date`
  2. Runs `rounds_per_discussion` rounds (each round walks every
     persona once)
  3. Calls `synthesize_conclusion` to produce the final conclusion
  4. Records the date in `completed_dates`

Cancellation: the worker re-reads the sweep row at the top of each
iteration. When `status='cancelled'` (set by the /cancel endpoint)
it bails out, leaving partial progress intact.

Concurrency: defaults to serial (1 discussion at a time). Operators
with generous LLM quotas can set `concurrency=2` or `3` — the
worker uses an asyncio.Semaphore to bound parallelism. Going
higher than 3 is rejected at API-input time because:
  - LLM provider rate limits (Anthropic / OpenAI tier-1 ~50 req/min)
    saturate fast with N-personas-per-round running concurrently
  - Daily quota per-user burns proportionally
  - M2.7 has known empty-response failure modes under load

The worker is a detached `asyncio.create_task` — it owns its own
DB session via `AsyncSessionLocal` so the request that fires
`/start` can return immediately.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.backtest_sweep import BacktestSweep

log = logging.getLogger(__name__)


# Status constants — narrower than free-form strings so typos
# surface at static-check time.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

# Bounds for input validation (enforced at API-input time too).
MAX_TRADING_DAYS = 60       # 60 days × N personas × LLM cost = real money
MAX_ROUNDS_PER_DISCUSSION = 5
MAX_CONCURRENCY = 3
LOOKAHEAD_CALENDAR_DAYS_PER_TRADING_DAY = 5   # weekend + holiday slack


# ── CRUD helpers ──────────────────────────────────────────────────


VALID_FOLD_KINDS: frozenset[str] = frozenset(
    {"train", "test", "production"}
)


async def create_sweep(
    db: AsyncSession,
    *,
    owner_id: UUID,
    topic: str,
    rules: str,
    market: str,
    persona_ids: list[str],
    anchor_date: date,
    trading_days_count: int,
    rounds_per_discussion: int = 1,
    concurrency: int = 1,
    auto_post_mortem: bool = True,
    strategy_id: UUID | None = None,
    fold_kind: str = "production",
    parent_sweep_id: UUID | None = None,
    weights_override: dict[str, float] | None = None,
) -> BacktestSweep:
    """Persist a new sweep row in `pending` state. Validates input
    bounds; raises ValueError on invalid input so the API layer
    can surface 400.

    `fold_kind` (PR-A0) labels the sweep as part of a walk-forward
    fold — `train` / `test` are spawned by the walk-forward
    orchestrator and link back at each other via `parent_sweep_id`.
    Direct API callers default to `production` and don't need to
    set either.

    `weights_override` (PR-A1) is the frozen persona weights from a
    completed train fold; the test fold spawned for that pair
    consults this map ahead of the parent strategy template so the
    OOS evaluation runs against the train-derived weights without
    leaking back into the template.
    """
    if not persona_ids:
        raise ValueError("persona_ids must not be empty")
    if trading_days_count < 1 or trading_days_count > MAX_TRADING_DAYS:
        raise ValueError(
            f"trading_days_count must be in [1, {MAX_TRADING_DAYS}]"
        )
    if (
        rounds_per_discussion < 1
        or rounds_per_discussion > MAX_ROUNDS_PER_DISCUSSION
    ):
        raise ValueError(
            f"rounds_per_discussion must be in [1, {MAX_ROUNDS_PER_DISCUSSION}]"
        )
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(
            f"concurrency must be in [1, {MAX_CONCURRENCY}]"
        )
    if not topic.strip() or not rules.strip():
        raise ValueError("topic and rules are required")
    if market not in ("TW", "US", "GLOBAL"):
        raise ValueError(f"market must be TW / US / GLOBAL, got {market!r}")
    if fold_kind not in VALID_FOLD_KINDS:
        raise ValueError(
            "fold_kind must be train / test / production, "
            f"got {fold_kind!r}"
        )
    if parent_sweep_id is not None and fold_kind == "production":
        # Production sweeps are top-level; only fold halves should
        # link to a sibling. This catches accidental misuse from
        # the API layer.
        raise ValueError(
            "parent_sweep_id is only valid when fold_kind is "
            "'train' or 'test'"
        )
    if weights_override is not None and fold_kind == "train":
        # Train folds learn their own weights from their own
        # discussions — overriding would short-circuit the very
        # thing the train fold exists to compute. Production is
        # allowed (PR-A2: auto-schedule injects walk-forward
        # validated weights), test is allowed (PR-A1: orchestrator
        # injects the frozen train weights).
        raise ValueError(
            "weights_override is not valid on a 'train' fold"
        )

    sweep = BacktestSweep(
        owner_id=owner_id,
        topic=topic.strip(),
        rules=rules.strip(),
        market=market,
        persona_ids=list(persona_ids),
        anchor_date=anchor_date,
        trading_days_count=trading_days_count,
        rounds_per_discussion=rounds_per_discussion,
        concurrency=concurrency,
        auto_post_mortem=auto_post_mortem,
        strategy_id=strategy_id,
        fold_kind=fold_kind,
        parent_sweep_id=parent_sweep_id,
        weights_override=weights_override,
    )
    db.add(sweep)
    await db.commit()
    await db.refresh(sweep)
    return sweep


async def list_sweeps(
    db: AsyncSession, *, owner_id: UUID, limit: int = 50,
) -> list[BacktestSweep]:
    """Most-recent-first list of an owner's sweeps."""
    stmt = (
        select(BacktestSweep)
        .where(BacktestSweep.owner_id == owner_id)
        .order_by(BacktestSweep.created_at.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


async def get_sweep(
    db: AsyncSession, *, sweep_id: UUID, owner_id: UUID,
) -> BacktestSweep | None:
    """Owner-scoped fetch — returns None when the row doesn't exist
    or belongs to someone else (404 from the API layer)."""
    return await db.scalar(
        select(BacktestSweep).where(
            BacktestSweep.id == sweep_id,
            BacktestSweep.owner_id == owner_id,
        )
    )


async def cancel_sweep(
    db: AsyncSession, sweep: BacktestSweep,
) -> BacktestSweep:
    """Flip the status to `cancelled`. The running worker will see
    this on its next iteration check and bail out, leaving
    `completed_dates` intact for resume / inspection.

    A sweep that's already in a terminal state (completed /
    cancelled / failed) is a no-op — re-cancelling shouldn't
    overwrite a successful completion timestamp."""
    if sweep.status in (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED):
        return sweep
    sweep.status = STATUS_CANCELLED
    sweep.cancelled_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(sweep)
    return sweep


async def delete_sweep(
    db: AsyncSession, sweep: BacktestSweep,
) -> None:
    """Hard-delete. Spawned discussions are NOT cascaded — they're
    independent rows the operator may still want to inspect.
    Caller is responsible for cancelling first if the sweep is
    actively running."""
    await db.delete(sweep)
    await db.commit()


# ── Trading-day resolution ────────────────────────────────────────


async def _resolve_trading_days(
    db: AsyncSession,
    *,
    market: str,
    anchor: date,
    count: int,
) -> list[date]:
    """Return the next `count` trading days from `anchor` inclusive,
    derived from `ohlcv_daily`. Naturally skips weekends + holidays.
    Returns the partial list when the archive doesn't reach `count`
    days ahead — caller decides whether to bail.

    Lookahead window: `count * 5` calendar days covers a worst-case
    (Lunar New Year + a holiday cluster) without scanning unbounded.
    """
    from models.ohlcv_daily import OhlcvDaily
    end = anchor + timedelta(
        days=count * LOOKAHEAD_CALENDAR_DAYS_PER_TRADING_DAY,
    )
    stmt = (
        select(OhlcvDaily.ts)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts >= anchor,
            OhlcvDaily.ts <= end,
        )
        .group_by(OhlcvDaily.ts)
        .order_by(OhlcvDaily.ts.asc())
        .limit(count)
    )
    rows = (await db.scalars(stmt)).all()
    return list(rows)


# ── Worker ────────────────────────────────────────────────────────


async def _process_one_date(
    sweep_id: UUID,
    target_date: date,
    *,
    semaphore: asyncio.Semaphore,
) -> tuple[date, str | None]:
    """Spawn a single discussion for `target_date` and run it through
    rounds + conclude. Returns `(target_date, error_or_None)` so the
    caller can update `completed_dates` / `failed_dates`.

    Each call opens its own DB session — concurrent calls write to
    independent sessions, no cross-task contention.
    """
    async with semaphore:
        try:
            async with AsyncSessionLocal() as db:
                # Re-read the sweep so the worker can pull the latest
                # template fields (in case the operator edited
                # mid-flight, even though we don't expose that today).
                sweep = await db.scalar(
                    select(BacktestSweep).where(
                        BacktestSweep.id == sweep_id,
                    )
                )
                if sweep is None:
                    return target_date, "sweep row deleted mid-run"

                # Lazy import — discussion_service pulls heavy LLM
                # plumbing; keeping it lazy means tests can stub
                # the path without paying the import cost.
                from services import discussion_service

                disc = await discussion_service.create_discussion(
                    db, owner_id=sweep.owner_id,
                    topic=sweep.topic, rules=sweep.rules,
                    persona_ids=list(sweep.persona_ids or []),
                    market=sweep.market,
                    as_of_date=target_date,
                    sweep_id=sweep.id,
                )
                # Run the configured number of rounds. Each round
                # consumes the round generator to completion.
                for _ in range(sweep.rounds_per_discussion):
                    async for _ev in discussion_service.run_round(
                        db, disc,
                        user_id=str(sweep.owner_id),
                        user_role="analyst",
                    ):
                        pass
                # Synthesize the original conclusion.
                await discussion_service.synthesize_conclusion(
                    db, disc, user_id=str(sweep.owner_id),
                )

                # PR #275: optional auto-post-mortem. Best-effort —
                # any failure here logs but does NOT fail the date
                # because the original conclusion already succeeded
                # (and is the verifier's anchor anyway).
                if sweep.auto_post_mortem:
                    try:
                        await _run_post_mortem_pass(db, disc, sweep.owner_id)
                    except Exception as pm_exc:
                        log.warning(
                            "backtest_sweep.post_mortem_failed",
                            extra={
                                "sweep_id": str(sweep_id),
                                "date": target_date.isoformat(),
                                "error": str(pm_exc),
                            },
                        )

                return target_date, None
        except Exception as exc:
            log.warning(
                "backtest_sweep.discussion_failed",
                extra={
                    "sweep_id": str(sweep_id),
                    "date": target_date.isoformat(),
                    "error": str(exc),
                },
            )
            return target_date, str(exc)


async def _run_post_mortem_pass(
    db: AsyncSession, discussion: Any, owner_id: UUID,
) -> None:
    """Inject the post-mortem critique prompt, run one reflection
    round, and re-synthesize. Mirrors the user-facing 「事後檢討」
    flow (PR #249 + PR #267 + PR #272 + PR #273) end-to-end.

    Skips silently when:
      - the original conclusion didn't materialise (run_round / synth
        failed before us — caller's exception handler captures it)
      - the post-mortem service finds no trading days past as_of
        (`build_post_mortem_message` returns an empty payload)

    The re-synthesize step routes the new conclusion to
    `post_mortem_conclusion` because the synthesizer's PR #272
    detection sees the injected user_input turn whose content
    starts with "【事後檢討".
    """
    from services import discussion_service
    from services.post_mortem_service import build_post_mortem_message

    # Refresh the discussion so we see the conclusion we just wrote.
    await db.refresh(discussion)
    if not discussion.conclusion:
        return

    payload = await build_post_mortem_message(db, discussion)
    if not payload.trading_days:
        return
    # Win-skip: recommendation already cleared the threshold so no
    # critique round is fired (saves N personas × LLM cost per
    # winning date during a multi-day sweep).
    if payload.verdict is not None and payload.verdict.status == "win":
        try:
            from middleware.metrics import POST_MORTEM_SKIPPED_TOTAL
            POST_MORTEM_SKIPPED_TOTAL.labels(market=discussion.market).inc()
        except Exception:
            pass
        return
    if not payload.prompt_text:
        return

    await discussion_service.inject_user_message(
        db, discussion, content=payload.prompt_text,
    )
    try:
        from middleware.metrics import POST_MORTEM_RAN_TOTAL
        POST_MORTEM_RAN_TOTAL.labels(market=discussion.market).inc()
    except Exception:
        pass
    # One reflection round so personas critique against the ground
    # truth surfaced in the prompt.
    async for _ev in discussion_service.run_round(
        db, discussion,
        user_id=str(owner_id),
        user_role="analyst",
    ):
        pass
    # Re-synthesize. The synthesizer's `has_post_mortem` branch
    # routes the output to `post_mortem_conclusion`, leaving the
    # original `conclusion` intact.
    await discussion_service.synthesize_conclusion(
        db, discussion, user_id=str(owner_id),
    )


async def _is_cancelled(sweep_id: UUID) -> bool:
    """Re-fetch the sweep status from the DB — the operator may have
    cancelled mid-flight. Cheap (single PK lookup) so we can call
    it on every iteration without worrying about overhead."""
    async with AsyncSessionLocal() as db:
        status = await db.scalar(
            select(BacktestSweep.status).where(
                BacktestSweep.id == sweep_id,
            )
        )
    return status == STATUS_CANCELLED


async def run_sweep_worker(sweep_id: UUID) -> None:
    """Background entry point fired by /start. Resolves trading
    days, then iterates them with the configured concurrency.

    All bookkeeping (status transitions, completed/failed lists,
    timestamps) lives here so the API layer can be a thin shell.
    Errors in any single discussion don't stop the sweep — they
    accumulate into `failed_dates` so the operator can retry just
    those dates afterward.
    """
    # Phase 1: resolve dates + flip status to running.
    async with AsyncSessionLocal() as db:
        sweep = await db.scalar(
            select(BacktestSweep).where(BacktestSweep.id == sweep_id)
        )
        if sweep is None:
            log.warning("backtest_sweep.start_missing_row",
                        extra={"sweep_id": str(sweep_id)})
            return
        if sweep.status not in (STATUS_PENDING,):
            log.info("backtest_sweep.start_skipped_non_pending",
                     extra={"sweep_id": str(sweep_id),
                            "status": sweep.status})
            return

        try:
            dates = await _resolve_trading_days(
                db, market=sweep.market,
                anchor=sweep.anchor_date,
                count=sweep.trading_days_count,
            )
        except Exception as exc:
            sweep.status = STATUS_FAILED
            sweep.error_message = f"resolve_trading_days failed: {exc}"
            sweep.completed_at = datetime.now(UTC)
            await db.commit()
            return

        if not dates:
            sweep.status = STATUS_FAILED
            sweep.error_message = (
                "No trading days resolved — `ohlcv_daily` archive may "
                "not reach the anchor date."
            )
            sweep.completed_at = datetime.now(UTC)
            await db.commit()
            return

        sweep.resolved_dates = [d.isoformat() for d in dates]
        sweep.status = STATUS_RUNNING
        sweep.started_at = datetime.now(UTC)
        concurrency = sweep.concurrency or 1
        await db.commit()

    # Phase 2: process dates with bounded concurrency. We launch one
    # asyncio task per date but the semaphore caps active workers
    # at `concurrency`. Each task opens its own DB session.
    semaphore = asyncio.Semaphore(concurrency)
    completed: list[str] = []
    failed: list[dict[str, Any]] = []
    cancelled_mid_flight = False

    # Iterate in chunks of `concurrency` so the cancel check fires
    # frequently. With concurrency=1 this is just sequential.
    pending_dates = list(dates)
    while pending_dates:
        if await _is_cancelled(sweep_id):
            cancelled_mid_flight = True
            break
        chunk = pending_dates[:concurrency]
        pending_dates = pending_dates[concurrency:]
        results = await asyncio.gather(
            *[
                _process_one_date(sweep_id, d, semaphore=semaphore)
                for d in chunk
            ],
            return_exceptions=False,
        )
        for d, err in results:
            if err is None:
                completed.append(d.isoformat())
            else:
                failed.append({"date": d.isoformat(), "error": err})
        # Persist incremental progress so the UI's polling shows
        # advancement without waiting for the whole sweep.
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(BacktestSweep)
                .where(BacktestSweep.id == sweep_id)
                .values(
                    completed_dates=list(completed),
                    failed_dates=list(failed),
                )
            )
            await db.commit()

    # Phase 3: finalise.
    async with AsyncSessionLocal() as db:
        sweep = await db.scalar(
            select(BacktestSweep).where(BacktestSweep.id == sweep_id)
        )
        if sweep is None:
            return
        if cancelled_mid_flight:
            # /cancel already wrote status + cancelled_at; just
            # persist the partial progress.
            sweep.completed_dates = list(completed)
            sweep.failed_dates = list(failed)
        else:
            sweep.status = STATUS_COMPLETED
            sweep.completed_at = datetime.now(UTC)
            sweep.completed_dates = list(completed)
            sweep.failed_dates = list(failed)
        await db.commit()

        # PR-C: re-learn persona weights for the parent strategy
        # template so the next sweep using it picks up the freshly
        # validated track record. Best-effort — any failure logs
        # but does NOT roll back the sweep completion (the operator
        # can recompute manually via POST /strategies/{id}/learn).
        #
        # PR-A1: skip the in-sample retraining when this sweep is
        # the test half of a walk-forward fold — letting test fold
        # results modify the template's weights would re-introduce
        # the very in-sample bias the OOS validation is meant to
        # surface. Train folds also skip (they hand off to the
        # walk-forward orchestrator's frozen-weights pipeline);
        # only `production` sweeps trigger the in-place learner.
        #
        # PR-A2: production sweeps that ran against a walk-forward
        # validated `weights_override` ALSO skip — the OOS-clean
        # weights are already in place, retraining in-sample on top
        # would re-poison them. Production sweeps WITHOUT a frozen
        # weights override (i.e. strategies that haven't run a
        # walk-forward yet) keep the legacy in-sample retrain so
        # the existing operator workflow stays intact during the
        # rollout.
        should_learn_weights = (
            not cancelled_mid_flight
            and sweep.strategy_id is not None
            and sweep.fold_kind == "production"
            and not sweep.weights_override
        )
        if should_learn_weights:
            try:
                from services import persona_weight_learner
                await persona_weight_learner.learn_weights_for_strategy(
                    db,
                    owner_id=sweep.owner_id,
                    strategy_id=sweep.strategy_id,
                )
            except Exception as exc:
                log.warning(
                    "backtest_sweep.learn_weights_failed",
                    extra={
                        "sweep_id": str(sweep_id),
                        "strategy_id": str(sweep.strategy_id),
                        "error": str(exc),
                    },
                )

            # PR-C2: refit the isotonic calibration curve from the
            # rolling pool of (confidence, outcome) pairs the parent
            # strategy has accumulated across its sweeps. Best-effort
            # — failure logs but doesn't roll back the sweep
            # completion, identical contract to the weight learner.
            # Sample-size gate inside the fitter handles the
            # cold-start case (strategy with <30 resolved pairs
            # gets `updated=False` and the curve stays NULL).
            try:
                from services import confidence_calibrator
                await confidence_calibrator.fit_isotonic_for_strategy(
                    db,
                    owner_id=sweep.owner_id,
                    strategy_id=sweep.strategy_id,
                )
            except Exception as exc:
                log.warning(
                    "backtest_sweep.fit_calibration_failed",
                    extra={
                        "sweep_id": str(sweep_id),
                        "strategy_id": str(sweep.strategy_id),
                        "error": str(exc),
                    },
                )


def start_sweep_in_background(sweep_id: UUID) -> asyncio.Task:
    """Public entry point for the API layer. Detaches the worker
    onto the running event loop so the HTTP request returns
    immediately. Returns the task handle (mainly for tests)."""
    return asyncio.create_task(run_sweep_worker(sweep_id))


__all__ = [
    "STATUS_PENDING", "STATUS_RUNNING", "STATUS_COMPLETED",
    "STATUS_CANCELLED", "STATUS_FAILED",
    "MAX_TRADING_DAYS", "MAX_ROUNDS_PER_DISCUSSION", "MAX_CONCURRENCY",
    "create_sweep", "list_sweeps", "get_sweep",
    "cancel_sweep", "delete_sweep",
    "run_sweep_worker", "start_sweep_in_background",
]
