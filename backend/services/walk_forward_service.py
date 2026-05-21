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
    # PR-4b: was the test fold's weights_override auto-promoted to
    # the strategy's live persona_weights? Populated by Phase 4.
    # `None` means promotion wasn't attempted (auto_promote_enabled
    # is False or evaluation failed); `False` means evaluated and
    # rejected; `True` means deployed.
    promoted: bool | None = None
    promotion_reason: str | None = None


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


async def has_active_walk_forward(
    db: AsyncSession,
    *,
    strategy_id: UUID,
) -> bool:
    """Audit follow-up #2: True iff this strategy already has a
    walk-forward run in flight — i.e. at least one train- or test-
    fold sweep is in pending or running status.

    The API endpoint consults this before scheduling a new
    orchestrator so two simultaneous /walk-forward POSTs don't
    each spawn parallel runs and race on weight learning.

    Production sweeps (`fold_kind='production'`) are intentionally
    NOT counted — those run independently of walk-forward and
    blocking on them would forbid the perfectly normal "auto-
    schedule cron is running production sweeps while operator
    triggers a walk-forward" workflow.
    """
    from services.backtest_sweep_service import (
        STATUS_PENDING, STATUS_RUNNING,
    )
    stmt = (
        select(BacktestSweep.id)
        .where(
            BacktestSweep.strategy_id == strategy_id,
            BacktestSweep.fold_kind.in_(("train", "test")),
            BacktestSweep.status.in_((STATUS_PENDING, STATUS_RUNNING)),
        )
        .limit(1)
    )
    return (await db.scalar(stmt)) is not None


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
    log.info(
        "walk_forward.start",
        extra={
            "strategy_id": str(strategy_id),
            "owner_id": str(owner_id),
            "n_folds": len(plan.folds),
            "market": plan.market,
        },
    )
    # Wrap the loop in a try/except so an unhandled exception
    # (e.g. AsyncSessionLocal can't connect, infrastructure
    # failure, KeyboardInterrupt during shutdown) gets logged at
    # WARNING level + emits the `failed` Prometheus counter
    # increment. Without this, asyncio.create_task swallows the
    # exception silently and the operator only learns by polling
    # `/sweeps?strategy_id=...` and seeing nothing happened.
    try:
        # Walk in chronological order (oldest fold first) so an
        # operator watching the dashboard sees the earliest
        # train/test pair finish first. Plan stores them anchor-
        # newest-first; reverse here.
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
                    extra={
                        "strategy_id": str(strategy_id),
                        "fold_index": fold.fold_index,
                        "error": str(exc),
                    },
                )
                result.error = str(exc)
            _record_fold_metric(result)
            results.append(result)
    except Exception as exc:
        log.exception(
            "walk_forward.orchestrator_failed",
            extra={
                "strategy_id": str(strategy_id),
                "owner_id": str(owner_id),
                "completed_folds": len(results),
                "error": str(exc),
            },
        )
        _record_run_metric("failed")
        raise

    failed_count = sum(1 for r in results if r.error is not None)
    if failed_count == 0:
        run_status = "success"
    elif failed_count == len(results):
        run_status = "failed"
    else:
        run_status = "partial"
    _record_run_metric(run_status)
    log.info(
        "walk_forward.complete",
        extra={
            "strategy_id": str(strategy_id),
            "owner_id": str(owner_id),
            "status": run_status,
            "folds_total": len(results),
            "folds_failed": failed_count,
        },
    )
    return results


def _record_fold_metric(result: FoldResult) -> None:
    try:
        from middleware.metrics import WALK_FORWARD_FOLDS_TOTAL
        outcome = "failed" if result.error is not None else "completed"
        WALK_FORWARD_FOLDS_TOTAL.labels(outcome=outcome).inc()
    except Exception:
        # Best-effort — metric infrastructure failure must never
        # disturb the orchestrator itself.
        pass


def _record_run_metric(status: str) -> None:
    try:
        from middleware.metrics import WALK_FORWARD_RUNS_TOTAL
        WALK_FORWARD_RUNS_TOTAL.labels(status=status).inc()
    except Exception:
        pass


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

    # Phase 1.5 — synchronously resolve verdicts on the train fold's
    # discussions BEFORE fitting weights. The architecture audit
    # caught this: without the verdict cron having run, every
    # train discussion's `verdict` is None → aggregate hit_rate
    # collapses to 0/N for every persona → `compute_weights_from_
    # aggregate` produces uniform weights → walk-forward becomes
    # a no-op (test fold runs with no override). The verifier task
    # is idempotent and safe to call synchronously in backtest
    # mode (`as_of_date != None`) because all required OHLCV
    # bars are already in the archive — the cron just hadn't
    # gotten around to it yet.
    await _verify_train_fold_discussions(train_sweep.id)

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

    # Phase 4 — PR-4b auto-promote. When the strategy has
    # `auto_promote_enabled=True` and the test fold's KPIs pass
    # both thresholds, write the OOS-validated weights straight to
    # the live `persona_weights` (and append a `record_version`
    # entry via the existing PR-4a hook). This closes the
    # "walk-forward result sits there waiting for human action"
    # loop without compromising the OOS-cleanness invariant —
    # it's the train→test→deploy progression, not a retrain on
    # the test results.
    try:
        promotion = await evaluate_walk_forward_for_promotion(
            owner_id=owner_id,
            strategy_id=strategy_id,
            train_sweep_id=train_sweep.id,
            test_sweep_id=test_sweep.id,
            weights=weights or {},
        )
        result.promoted = bool(promotion.get("promoted"))
        result.promotion_reason = promotion.get("reason")
    except Exception as exc:
        log.warning(
            "walk_forward.auto_promote_failed",
            extra={
                "test_sweep_id": str(test_sweep.id),
                "strategy_id": str(strategy_id),
                "error": str(exc),
            },
        )
    return result


async def evaluate_walk_forward_for_promotion(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    train_sweep_id: UUID,
    test_sweep_id: UUID,
    weights: dict[str, float],
) -> dict[str, Any]:
    """PR-4b: decide whether the test fold's frozen weights should
    be promoted to the strategy's live `persona_weights`.

    Returns:
      {
        "promoted": bool,
        "reason": str,                # why / why not
        "test_brier": float | None,
        "test_win_rate": float | None,
        "baseline_brier": float | None,  # the strategy's pre-promote curve, for context
      }

    Skipped (returns `promoted=False`) when:
      - `auto_promote_enabled=False` on the strategy template
      - test fold KPIs are insufficient (verdict resolution failed)
      - test brier improvement < `auto_promote_min_oos_brier_improvement`
      - test win rate < `auto_promote_min_oos_hit_rate`
      - the supplied `weights` map is empty (defensive — shouldn't
        happen in normal flow but the bare `return` keeps us safe)

    On promotion:
      - writes `weights` to the template's live `persona_weights`
      - calls `strategy_version_service.record_version` with the
        notes describing the OOS provenance + the train sweep id
    """
    from services import strategy_version_service
    from services import sweep_aggregate_service as agg
    from services import strategy_template_service as tsvc
    from sqlalchemy import select as _sa_select

    # Resolve verdicts on the TEST fold so the aggregate sees real
    # win/loss counts. Mirrors Phase 1.5's pattern but on the test
    # sweep — without this the test fold's `win_rate` collapses to
    # 0/N which would block legitimate promotions.
    await _verify_test_fold_discussions(test_sweep_id)

    async with AsyncSessionLocal() as db:
        tmpl = await db.scalar(
            _sa_select(DiscussionStrategyTemplate).where(
                DiscussionStrategyTemplate.id == strategy_id,
                DiscussionStrategyTemplate.owner_id == owner_id,
            )
        )
        if tmpl is None:
            return {
                "promoted": False,
                "reason": "strategy_not_found",
                "test_brier": None,
                "test_win_rate": None,
                "baseline_brier": None,
            }
        if not tmpl.auto_promote_enabled:
            return {
                "promoted": False,
                "reason": "auto_promote_disabled",
                "test_brier": None,
                "test_win_rate": None,
                "baseline_brier": None,
            }
        if not weights:
            return {
                "promoted": False,
                "reason": "empty_weights",
                "test_brier": None,
                "test_win_rate": None,
                "baseline_brier": None,
            }

        from models.backtest_sweep import BacktestSweep
        test_sweep = await db.scalar(
            _sa_select(BacktestSweep).where(
                BacktestSweep.id == test_sweep_id,
            )
        )
        if test_sweep is None:
            return {
                "promoted": False, "reason": "test_sweep_missing",
                "test_brier": None, "test_win_rate": None,
                "baseline_brier": None,
            }

        test_payload = await agg.aggregate_sweep(db, test_sweep)
        test_brier = test_payload.get("brier_score")
        test_win_rate = test_payload.get("win_rate")
        # Baseline = the strategy's pre-promote brier (the live
        # curve's mean brier over the recent window). Use the test
        # fold's own pre-fit baseline by reading from the strategy's
        # current health snapshot if present, otherwise None.
        baseline_brier = None
        try:
            from services import strategy_health_service as hsvc
            recent = await hsvc.list_recent_snapshots(
                db, strategy_id=strategy_id, days=30,
            )
            for r in recent:
                if r.brier_30d is not None:
                    baseline_brier = float(r.brier_30d)
                    break
        except Exception:
            pass

        if test_brier is None or test_win_rate is None:
            return {
                "promoted": False,
                "reason": "test_fold_kpi_unresolved",
                "test_brier": test_brier,
                "test_win_rate": test_win_rate,
                "baseline_brier": baseline_brier,
            }
        if test_win_rate < tmpl.auto_promote_min_oos_hit_rate:
            return {
                "promoted": False,
                "reason": (
                    f"test_win_rate {test_win_rate:.3f} < threshold "
                    f"{tmpl.auto_promote_min_oos_hit_rate:.3f}"
                ),
                "test_brier": test_brier,
                "test_win_rate": test_win_rate,
                "baseline_brier": baseline_brier,
            }
        # Brier improvement: lower is better. Require baseline -
        # test ≥ threshold. When no baseline is available (cold
        # strategy), interpret threshold=0 as "any non-degraded
        # brier qualifies"; otherwise we'd lock out the first
        # auto-promote forever waiting for a baseline.
        if baseline_brier is not None:
            improvement = baseline_brier - test_brier
            if improvement < tmpl.auto_promote_min_oos_brier_improvement:
                return {
                    "promoted": False,
                    "reason": (
                        f"brier improvement {improvement:.3f} < threshold "
                        f"{tmpl.auto_promote_min_oos_brier_improvement:.3f}"
                    ),
                    "test_brier": test_brier,
                    "test_win_rate": test_win_rate,
                    "baseline_brier": baseline_brier,
                }

        # All gates passed — promote. Live-column write goes
        # through `set_persona_weights` to share the existing
        # commit + invalidate flow, then we record the version.
        await tsvc.set_persona_weights(db, tmpl, weights=weights)
        try:
            await strategy_version_service.record_version(
                db,
                strategy_id=strategy_id,
                artifact_kind="weights",
                payload=weights,
                source_sweep_id=test_sweep_id,
                notes=(
                    f"auto-promoted from walk-forward test fold "
                    f"(train={train_sweep_id}, test={test_sweep_id}); "
                    f"brier={test_brier:.3f}, win_rate={test_win_rate:.3f}"
                ),
            )
        except Exception as exc:
            log.warning(
                "walk_forward.auto_promote.version_record_failed",
                extra={
                    "strategy_id": str(strategy_id),
                    "test_sweep_id": str(test_sweep_id),
                    "error": str(exc),
                },
            )
        return {
            "promoted": True,
            "reason": "kpis_passed",
            "test_brier": test_brier,
            "test_win_rate": test_win_rate,
            "baseline_brier": baseline_brier,
        }


async def _verify_test_fold_discussions(test_sweep_id: UUID) -> int:
    """Mirror of `_verify_train_fold_discussions` for the test fold.
    Same idempotent + best-effort contract — without it the test
    fold's aggregate `win_rate` is 0/N (every discussion's verdict
    still pending) and the auto-promote gate trivially fails
    'test_fold_kpi_unresolved'."""
    from sqlalchemy import select as _select

    from models.discussion import Discussion as _Discussion
    from tasks.verify_discussion_outcome import _resolve_thresholds, _verify_one

    resolved = 0
    async with AsyncSessionLocal() as db:
        big_win_pct, win_pct, big_loss_pct = await _resolve_thresholds(db)
        rows = (await db.scalars(
            _select(_Discussion).where(
                _Discussion.sweep_id == test_sweep_id,
            )
        )).all()
        for d in rows:
            if d.verdict is not None:
                continue
            try:
                ok = await _verify_one(
                    db, d,
                    big_win_pct=big_win_pct,
                    win_pct=win_pct,
                    big_loss_pct=big_loss_pct,
                )
                if ok:
                    resolved += 1
            except Exception as exc:
                log.warning(
                    "walk_forward.test_verify_failed",
                    extra={
                        "discussion_id": str(d.id),
                        "test_sweep_id": str(test_sweep_id),
                        "error": str(exc),
                    },
                )
    return resolved


async def _verify_train_fold_discussions(train_sweep_id: UUID) -> int:
    """Walk every discussion in the train sweep and run the
    verdict resolver synchronously so `_fit_frozen_weights`
    can read meaningful hit rates from the aggregate.

    Returns the count of discussions that received a verdict on
    this pass (those that didn't either had no symbols / had
    insufficient bars / or already had a verdict from a prior
    cron tick).

    The verifier (`tasks.verify_discussion_outcome._verify_one`)
    is idempotent — it short-circuits when verdict is already
    set — and reads OHLCV directly from the archive in backtest
    mode, so calling N times against N already-completed
    discussions has predictable upper-bound cost.

    Failures are isolated per discussion: one bad row (e.g.
    OHLCV gap that the cron's stale-grace path would normally
    flush) doesn't block the rest. The fold itself stays
    valid even if some verdicts couldn't resolve — the
    aggregate just sees them as `pending` and PR-C's
    eligibility gate filters them out.
    """
    from sqlalchemy import select as _select

    from models.discussion import Discussion as _Discussion
    from tasks.verify_discussion_outcome import _resolve_thresholds, _verify_one

    resolved = 0
    async with AsyncSessionLocal() as db:
        big_win_pct, win_pct, big_loss_pct = await _resolve_thresholds(db)
        rows = (await db.scalars(
            _select(_Discussion).where(
                _Discussion.sweep_id == train_sweep_id,
            )
        )).all()
        for d in rows:
            if d.verdict is not None:
                continue   # already verified, skip
            try:
                ok = await _verify_one(
                    db, d,
                    big_win_pct=big_win_pct,
                    win_pct=win_pct,
                    big_loss_pct=big_loss_pct,
                )
                if ok:
                    resolved += 1
            except Exception as exc:
                log.warning(
                    "walk_forward.train_verify_failed",
                    extra={
                        "discussion_id": str(d.id),
                        "train_sweep_id": str(train_sweep_id),
                        "error": str(exc),
                    },
                )
    log.info(
        "walk_forward.train_verified",
        extra={
            "train_sweep_id": str(train_sweep_id),
            "resolved": resolved,
            "total": len(rows),
        },
    )
    return resolved


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
    "has_active_walk_forward",
]
