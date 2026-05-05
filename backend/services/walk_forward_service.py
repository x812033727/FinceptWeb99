"""Walk-forward orchestrator (PR-A1).

Builds rolling-window train/test fold plans from a strategy
template's history and drives the train→fit→test pipeline so
persona weights learned on one slice of dates get evaluated on a
disjoint future slice. Closes the in-sample loophole that the
existing `run_sweep_worker` Phase 3 retraining was opening.

Default: rolling 60d train + 20d test, anchor = today (or
operator-supplied), N folds (default 2). Each fold's test sweep
links back at its train sibling via `parent_sweep_id` so the
aggregate UI can render side-by-side KPIs and PR-A2's frozen-
weights resolver can find the right train run.

Lifecycle:
  1. `plan_walk_forward` resolves the trading-day windows from
     `ohlcv_daily`. Pure read; rejects with ValueError when the
     archive can't reach the requested span (caller surfaces 400).
  2. `execute_walk_forward_in_background` is the public
     fire-and-forget entry. Detaches an asyncio task that walks
     the folds in order: build train sweep → run worker (await
     completion) → fit weights from train aggregate → build test
     sweep with `weights_override` → run worker.

The orchestrator does NOT mutate the parent strategy template.
That distinction is what keeps the OOS evaluation clean — train
weights stay scoped to the test sibling alone, the template's
own `persona_weights` continues to track the operator's manual
in-sample workflow until PR-A2 promotes the OOS-validated values
into production sweeps.

Failure isolation: a fold whose train OR test sweep fails is
logged and the orchestrator continues to the next fold. The
caller's per-fold result list carries `error` strings for the
failed legs so the dashboard can show partial progress.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from models.backtest_sweep import BacktestSweep
from models.discussion_strategy_template import DiscussionStrategyTemplate

log = logging.getLogger(__name__)


DEFAULT_TRAIN_WINDOW_DAYS = 60
DEFAULT_TEST_WINDOW_DAYS = 20
DEFAULT_N_FOLDS = 2
MAX_FOLDS = 6   # 6 folds × (60+20) = 480 trading days ≈ 2 years
MAX_WINDOW_DAYS = 120


@dataclass
class WalkForwardFold:
    """One (train, test) tuple in a rolling-window plan."""
    fold_index: int
    train_anchor: date
    train_dates: list[date]
    test_anchor: date
    test_dates: list[date]


@dataclass
class WalkForwardPlan:
    strategy_id: UUID
    market: str
    train_window_days: int
    test_window_days: int
    folds: list[WalkForwardFold] = field(default_factory=list)


@dataclass
class FoldResult:
    fold_index: int
    train_sweep_id: UUID | None = None
    test_sweep_id: UUID | None = None
    error: str | None = None


async def plan_walk_forward(
    db: AsyncSession,
    *,
    strategy_id: UUID,
    market: str,
    anchor_date: date,
    train_window_days: int = DEFAULT_TRAIN_WINDOW_DAYS,
    test_window_days: int = DEFAULT_TEST_WINDOW_DAYS,
    n_folds: int = DEFAULT_N_FOLDS,
) -> WalkForwardPlan:
    """Resolve `n_folds` rolling (train, test) tuples ending on or
    before `anchor_date`.

    Layout (most recent fold first to make `latest_validated_weights`
    reads cheap, and the worker walks them in reverse to run the
    earliest fold first chronologically):

        ... ──┬─ fold[1] ─┬─────── fold[0] ─────── anchor
              │           │
            train         test  train     test
              ◄────────►◄──────►◄──────►◄──────►

    Raises ValueError when the archive can't reach the requested
    span — the API layer surfaces 400 with the message verbatim.
    """
    if train_window_days < 1 or train_window_days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"train_window_days must be in [1, {MAX_WINDOW_DAYS}]"
        )
    if test_window_days < 1 or test_window_days > MAX_WINDOW_DAYS:
        raise ValueError(
            f"test_window_days must be in [1, {MAX_WINDOW_DAYS}]"
        )
    if n_folds < 1 or n_folds > MAX_FOLDS:
        raise ValueError(f"n_folds must be in [1, {MAX_FOLDS}]")
    if market not in ("TW", "US", "GLOBAL"):
        raise ValueError(f"market must be TW / US / GLOBAL, got {market!r}")

    fold_span = train_window_days + test_window_days
    earliest_needed = anchor_date - timedelta(
        days=fold_span * n_folds * 2,   # generous calendar buffer
    )
    trading_days = await _resolve_trading_days_in_range(
        db, market=market,
        start=earliest_needed, end=anchor_date,
    )
    if len(trading_days) < fold_span * n_folds:
        raise ValueError(
            f"ohlcv_daily archive does not reach far enough back to "
            f"build {n_folds} fold(s) of {train_window_days}+"
            f"{test_window_days} trading days each. Archive has "
            f"{len(trading_days)} bars; need {fold_span * n_folds}."
        )

    # Walk backwards from anchor: fold[0]'s test ends on anchor,
    # fold[0]'s train ends right before fold[0]'s test starts.
    # fold[1]'s test ends right before fold[0]'s train starts, etc.
    folds: list[WalkForwardFold] = []
    cursor_end_idx = len(trading_days)   # exclusive
    for i in range(n_folds):
        test_end_idx = cursor_end_idx
        test_start_idx = test_end_idx - test_window_days
        train_end_idx = test_start_idx
        train_start_idx = train_end_idx - train_window_days
        if train_start_idx < 0:
            # Defensive — already validated above, but in case the
            # archive has gaps we hit here.
            break
        train_dates = trading_days[train_start_idx:train_end_idx]
        test_dates = trading_days[test_start_idx:test_end_idx]
        folds.append(WalkForwardFold(
            fold_index=i,
            train_anchor=train_dates[0],
            train_dates=train_dates,
            test_anchor=test_dates[0],
            test_dates=test_dates,
        ))
        cursor_end_idx = train_start_idx

    return WalkForwardPlan(
        strategy_id=strategy_id,
        market=market,
        train_window_days=train_window_days,
        test_window_days=test_window_days,
        folds=folds,
    )


async def _resolve_trading_days_in_range(
    db: AsyncSession,
    *,
    market: str,
    start: date,
    end: date,
) -> list[date]:
    """All distinct trading days in `[start, end]` from `ohlcv_daily`,
    ascending. Reuses the same cardinality semantics as
    `_resolve_trading_days` in backtest_sweep_service but bounded
    by an explicit start so we can run far back enough to seed N
    folds.
    """
    from models.ohlcv_daily import OhlcvDaily
    stmt = (
        select(OhlcvDaily.ts)
        .where(
            OhlcvDaily.market == market,
            OhlcvDaily.ts >= start,
            OhlcvDaily.ts <= end,
        )
        .group_by(OhlcvDaily.ts)
        .order_by(OhlcvDaily.ts.asc())
    )
    rows = (await db.scalars(stmt)).all()
    return [r for r in rows]


# Type alias for the worker callable so tests can inject a stub
# without monkeypatching the import. Default: the production
# `run_sweep_worker`.
SweepWorker = Callable[[UUID], Awaitable[None]]


async def execute_walk_forward(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    plan: WalkForwardPlan,
    rounds_per_discussion: int = 1,
    concurrency: int = 1,
    auto_post_mortem: bool = True,
    sweep_worker: SweepWorker | None = None,
) -> list[FoldResult]:
    """Drive the train→fit→test pipeline for every fold in `plan`.

    Awaits each sweep worker in turn so the test fold reliably sees
    the train fold's completed aggregate. Failures are isolated per
    fold — one bad fold doesn't halt the others. Returns the per-
    fold result list with sweep IDs (for the dashboard) and any
    error strings.

    `sweep_worker` is injectable for tests — production code passes
    the real `run_sweep_worker` from backtest_sweep_service.
    """
    if sweep_worker is None:
        from services.backtest_sweep_service import run_sweep_worker
        sweep_worker = run_sweep_worker

    results: list[FoldResult] = []
    # Walk in chronological order (oldest fold first) so an operator
    # watching the dashboard sees the earliest train/test pair finish
    # first. Plan stores them anchor-newest-first; reverse here.
    for fold in reversed(plan.folds):
        result = FoldResult(fold_index=fold.fold_index)
        try:
            result = await _run_one_fold(
                fold=fold,
                owner_id=owner_id,
                strategy_id=strategy_id,
                market=plan.market,
                rounds_per_discussion=rounds_per_discussion,
                concurrency=concurrency,
                auto_post_mortem=auto_post_mortem,
                sweep_worker=sweep_worker,
            )
        except Exception as exc:
            log.exception(
                "walk_forward.fold_failed",
                extra={"fold_index": fold.fold_index, "error": str(exc)},
            )
            result.error = str(exc)
        results.append(result)
    return results


async def _run_one_fold(
    *,
    fold: WalkForwardFold,
    owner_id: UUID,
    strategy_id: UUID,
    market: str,
    rounds_per_discussion: int,
    concurrency: int,
    auto_post_mortem: bool,
    sweep_worker: SweepWorker,
) -> FoldResult:
    """One fold = one train sweep + one test sweep. The train sweep
    runs first; weights are fitted from its aggregate (NOT written
    to the template — that's the whole point); the test sweep runs
    with those weights as a `weights_override`."""
    from services.backtest_sweep_service import create_sweep

    result = FoldResult(fold_index=fold.fold_index)

    # Phase 1: spawn + run the train sweep.
    async with AsyncSessionLocal() as db:
        strategy = await db.scalar(
            select(DiscussionStrategyTemplate).where(
                DiscussionStrategyTemplate.id == strategy_id,
                DiscussionStrategyTemplate.owner_id == owner_id,
            )
        )
        if strategy is None:
            result.error = f"strategy {strategy_id} not found"
            return result

        train_sweep = await create_sweep(
            db,
            owner_id=owner_id,
            topic=strategy.topic,
            rules=strategy.rules,
            market=strategy.market,
            persona_ids=list(strategy.persona_ids or []),
            anchor_date=fold.train_anchor,
            trading_days_count=len(fold.train_dates),
            rounds_per_discussion=rounds_per_discussion,
            concurrency=concurrency,
            auto_post_mortem=auto_post_mortem,
            strategy_id=strategy_id,
            fold_kind="train",
        )
        result.train_sweep_id = train_sweep.id

    await sweep_worker(train_sweep.id)

    # Phase 2: fit weights from the completed train sweep aggregate
    # WITHOUT writing them back to the template.
    weights = await _fit_frozen_weights(
        owner_id=owner_id, strategy_id=strategy_id,
        train_sweep_id=train_sweep.id,
    )

    # Phase 3: spawn + run the test sweep with the frozen weights.
    async with AsyncSessionLocal() as db:
        test_sweep = await create_sweep(
            db,
            owner_id=owner_id,
            topic=strategy.topic,
            rules=strategy.rules,
            market=strategy.market,
            persona_ids=list(strategy.persona_ids or []),
            anchor_date=fold.test_anchor,
            trading_days_count=len(fold.test_dates),
            rounds_per_discussion=rounds_per_discussion,
            concurrency=concurrency,
            auto_post_mortem=auto_post_mortem,
            strategy_id=strategy_id,
            fold_kind="test",
            parent_sweep_id=train_sweep.id,
            weights_override=weights or None,
        )
        result.test_sweep_id = test_sweep.id

    await sweep_worker(test_sweep.id)
    return result


async def _fit_frozen_weights(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    train_sweep_id: UUID,
) -> dict[str, float]:
    """Run the persona weight aggregation on a single completed
    train sweep, returning the {persona_id: weight} map WITHOUT
    persisting it to the strategy template.

    Reuses `persona_weight_learner.compute_weights_from_aggregate`
    so the math (Laplace smoothing, [0.5, 2.0] bounds,
    MIN_SAMPLES gate) stays identical to the in-sample path.
    Empty dict on any error — the test sweep then runs with the
    template's existing weights or no weights at all, which is
    still a valid OOS check (just less interesting).
    """
    try:
        from services import persona_weight_learner as plw
        from services import sweep_aggregate_service as agg
        async with AsyncSessionLocal() as db:
            sweep = await db.scalar(
                select(BacktestSweep).where(
                    BacktestSweep.id == train_sweep_id,
                    BacktestSweep.owner_id == owner_id,
                )
            )
            if sweep is None:
                return {}
            payload = await agg.aggregate_sweep(db, sweep)
        per_persona = payload.get("per_persona") or []
        # Mirror the eligibility gate from `learn_weights_for_strategy`
        # so a persona with <MIN_SAMPLES discussions in the train
        # window doesn't get a noisy weight.
        eligible = [
            p for p in per_persona
            if p.get("discussions_count", 0) >= plw.MIN_SAMPLES
            and p.get("hit_rate") is not None
        ]
        if not eligible:
            return {}
        return plw.compute_weights_from_aggregate(eligible)
    except Exception as exc:
        log.warning(
            "walk_forward.weight_fit_failed",
            extra={
                "strategy_id": str(strategy_id),
                "train_sweep_id": str(train_sweep_id),
                "error": str(exc),
            },
        )
        return {}


def execute_walk_forward_in_background(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    plan: WalkForwardPlan,
    rounds_per_discussion: int = 1,
    concurrency: int = 1,
    auto_post_mortem: bool = True,
) -> asyncio.Task[Any]:
    """Public fire-and-forget entry point for the API layer.

    Returns the asyncio.Task handle; tests may want to await it,
    production callers ignore it. The wrapper detaches the
    orchestrator so the HTTP request creating the walk-forward
    can return immediately.
    """
    coro = execute_walk_forward(
        owner_id=owner_id,
        strategy_id=strategy_id,
        plan=plan,
        rounds_per_discussion=rounds_per_discussion,
        concurrency=concurrency,
        auto_post_mortem=auto_post_mortem,
    )
    return asyncio.create_task(coro)


__all__ = [
    "DEFAULT_TRAIN_WINDOW_DAYS",
    "DEFAULT_TEST_WINDOW_DAYS",
    "DEFAULT_N_FOLDS",
    "MAX_FOLDS",
    "MAX_WINDOW_DAYS",
    "WalkForwardFold",
    "WalkForwardPlan",
    "FoldResult",
    "plan_walk_forward",
    "execute_walk_forward",
    "execute_walk_forward_in_background",
]
