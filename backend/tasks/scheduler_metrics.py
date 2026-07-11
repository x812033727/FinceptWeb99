"""
APScheduler → Prometheus bridge.

APScheduler 3.x execution events don't carry a duration, so we key a
monotonic start time by job_id at EVENT_JOB_SUBMITTED and take the
delta when EVENT_JOB_EXECUTED / EVENT_JOB_ERROR arrives. Every job in
tasks.scheduler registers with max_instances=1, so at most one run of
a given job_id is in flight and the keying is unambiguous. A missed
event (coalesced or skipped run) counts toward outcome="missed" and
never observes a duration.

Attach once per scheduler instance from setup_jobs(); listeners are
process-local, matching the per-process scheduler itself.
"""
import time

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)

from middleware.metrics import (
    SCHEDULER_JOB_DURATION_SECONDS,
    SCHEDULER_JOB_RUNS_TOTAL,
)

_started_at: dict[str, float] = {}


def _on_submitted(event) -> None:
    _started_at[event.job_id] = time.monotonic()


def _on_finished(event) -> None:
    outcome = "error" if getattr(event, "exception", None) else "ok"
    SCHEDULER_JOB_RUNS_TOTAL.labels(event.job_id, outcome).inc()
    started = _started_at.pop(event.job_id, None)
    if started is not None:
        SCHEDULER_JOB_DURATION_SECONDS.labels(event.job_id).observe(
            time.monotonic() - started
        )


def _on_missed(event) -> None:
    SCHEDULER_JOB_RUNS_TOTAL.labels(event.job_id, "missed").inc()
    _started_at.pop(event.job_id, None)


def attach_metrics(scheduler) -> None:
    scheduler.add_listener(_on_submitted, EVENT_JOB_SUBMITTED)
    scheduler.add_listener(_on_finished, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)
