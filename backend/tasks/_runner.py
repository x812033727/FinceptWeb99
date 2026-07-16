"""Shared control-flow skeleton for the ingest cron tasks (R3-A3).

Every ``tasks/ingest_*.py`` hand-copied the same ~40-line wrapper
around its fetch+persist body: acquire a Redis SET-NX lock, gate on the
exponential-backoff cooldown, run the body, then record health and
arm-or-clear the failure counter, and always release the lock. This
module extracts that skeleton verbatim so the structurally identical
tasks share ONE implementation.

The lock / health collaborators are *injected* by the calling ``run()``
rather than imported here. Each task passes its own module-global
``acquire_lock`` / ``record_health`` / ... bindings, which the runner
then calls. This keeps the originals patchable at the task-module
namespace — the existing test suites do
``patch("tasks.ingest_ohlcv_tw.record_health", ...)`` and the patched
binding is picked up because ``run()`` reads the module global at call
time before handing it to the runner.

A task opts in by::

    from tasks._runner import TaskOutcome, run_ingest_task

    async def _body() -> TaskOutcome:
        return TaskOutcome(row_count=await _do_run())

    async def run() -> None:
        await run_ingest_task(
            job_id=JOB_ID, lock_key=_LOCK_KEY, lock_ttl=_LOCK_TTL, log=log,
            acquire_lock=acquire_lock, release_lock=release_lock,
            backoff_remaining_seconds=backoff_remaining_seconds,
            get_failure_count=get_failure_count, get_health=get_health,
            record_health=record_health, record_failure=record_failure,
            clear_failures=clear_failures,
            body=_body, format_error=_format_error,
        )

The runner reproduces the canonical flow EXACTLY:

  * lock not acquired      -> log ``.skipped_lock_held`` and return
                              (the lock is NOT released — it isn't ours)
  * active backoff window  -> record a ``skipped (backoff ...)`` health
                              row preserving the last real error, return
  * body raises            -> ``record_failure`` + record an unhealthy
                              ``auto-backoff armed`` health row, swallow
  * body succeeds          -> ``clear_failures`` + record ``ok=True``
  * finally                -> release the lock

Tasks with bespoke pre-exception branches (FinMind paywall / silent-
deny detection) or multi-sub-step aggregation do NOT fit this skeleton
and keep their hand-written ``run()``.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class TaskOutcome:
    """Success payload returned by a task body.

    The runner records it via ``record_health(ok=True, ...)``. Optional
    fields cover the task-specific enrichments the originals carried:

      * ``latest_data_ts`` — feeds the data-freshness Prometheus gauge
        (ohlcv / fundamentals / taiex_history). ``None`` is identical to
        omitting the kwarg.
      * ``status`` — a human-readable summary stored in the health
        ``error`` column even on success (news / announcements tasks
        surface their fetched/attempted counters this way). ``None`` is
        identical to omitting the kwarg.
      * ``done_extra`` — structured payload for the task's ``.done`` info
        log; defaults to the canonical ``{"rows_processed": row_count}``.
      * ``empty_is_stale`` — set when the body asked upstream for data it
        expected to exist. A ``row_count`` of 0 then records ``ok=False``
        instead of a green tick. Only the body knows the difference
        between "upstream gave us nothing" and "there was nothing to
        ask for" (a holiday, a window already archived), so the runner
        can't infer it. Defaults to today's behaviour: 0 rows is ok.
    """

    row_count: int
    latest_data_ts: date | datetime | None = None
    status: str | None = None
    done_extra: dict | None = None
    empty_is_stale: bool = False

    def log_extra(self) -> dict:
        if self.done_extra is not None:
            return self.done_extra
        return {"rows_processed": self.row_count}


async def run_ingest_task(
    *,
    job_id: str,
    lock_key: str,
    lock_ttl: int,
    log: logging.Logger,
    acquire_lock: Callable[..., Awaitable[bool]],
    release_lock: Callable[..., Awaitable[object]],
    backoff_remaining_seconds: Callable[[str], Awaitable[int]],
    get_failure_count: Callable[[str], Awaitable[int]],
    get_health: Callable[[str], Awaitable[object]],
    record_health: Callable[..., Awaitable[object]],
    record_failure: Callable[[str], Awaitable[int]],
    clear_failures: Callable[[str], Awaitable[object]],
    body: Callable[[], Awaitable[TaskOutcome]],
    format_error: Callable[[BaseException], str],
    log_backoff_skip: bool = False,
) -> None:
    """Run ``body`` under the shared lock / backoff / health skeleton.

    The lock / health collaborators are injected (see module docstring)
    so the caller's module-global bindings — and any test monkeypatches
    of them — are the ones invoked.

    ``log_backoff_skip`` mirrors the handful of tasks (news / announce-
    ments) that emit an extra ``.skipped_backoff`` info log inside the
    cooldown gate; the majority omit it.
    """
    if not await acquire_lock(lock_key, lock_ttl):
        log.info(f"{job_id}.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(job_id)
        if remaining > 0:
            failures = await get_failure_count(job_id)
            mins = max(1, remaining // 60)
            previous = await get_health(job_id)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                tail = f"; last: {previous.error[:200]}"
            if log_backoff_skip:
                log.info(
                    f"{job_id}.skipped_backoff",
                    extra={"failures": failures, "seconds_remaining": remaining},
                )
            await record_health(
                job_id, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining{tail})"
                ),
            )
            return

        try:
            outcome = await body()
        except Exception as exc:
            detail = format_error(exc)
            failures = await record_failure(job_id)
            log.warning(
                f"{job_id}.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                job_id, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        # The upstream answered, so no failure to arm a backoff on — a
        # cooldown would only delay tomorrow's attempt, which is the
        # opposite of what an empty table needs.
        await clear_failures(job_id)
        log.info(f"{job_id}.done", extra=outcome.log_extra())
        if outcome.row_count == 0 and outcome.empty_is_stale:
            log.warning(f"{job_id}.empty_result")
            await record_health(
                job_id, ok=False, row_count=0,
                latest_data_ts=outcome.latest_data_ts,
                error=(
                    "upstream returned no rows for a window that should "
                    "have carried some"
                    + (f" — {outcome.status}" if outcome.status else "")
                ),
            )
            return
        await record_health(
            job_id, ok=True, row_count=outcome.row_count,
            latest_data_ts=outcome.latest_data_ts, error=outcome.status,
        )
    finally:
        await release_lock(lock_key)
