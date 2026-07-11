"""Tests for the APScheduler → Prometheus bridge (tasks.scheduler_metrics)."""
from types import SimpleNamespace

from tasks import scheduler_metrics
from middleware.metrics import (
    SCHEDULER_JOB_DURATION_SECONDS,
    SCHEDULER_JOB_RUNS_TOTAL,
)


def _runs_value(job_id: str, outcome: str) -> float:
    return SCHEDULER_JOB_RUNS_TOTAL.labels(job_id, outcome)._value.get()


def _duration_count(job_id: str) -> float:
    # Histogram child exposes the observation count via _sum/_buckets;
    # collect() is the stable public surface.
    for metric in SCHEDULER_JOB_DURATION_SECONDS.collect():
        for sample in metric.samples:
            if (
                sample.name.endswith("_count")
                and sample.labels.get("job_id") == job_id
            ):
                return sample.value
    return 0.0


def test_submitted_then_executed_records_duration_and_ok():
    event = SimpleNamespace(job_id="job_ok", exception=None)
    before = _runs_value("job_ok", "ok")

    scheduler_metrics._on_submitted(event)
    assert "job_ok" in scheduler_metrics._started_at
    scheduler_metrics._on_finished(event)

    assert _runs_value("job_ok", "ok") == before + 1
    assert _duration_count("job_ok") == 1
    assert "job_ok" not in scheduler_metrics._started_at


def test_error_outcome_counted_separately():
    event = SimpleNamespace(job_id="job_err", exception=RuntimeError("boom"))
    before = _runs_value("job_err", "error")

    scheduler_metrics._on_submitted(event)
    scheduler_metrics._on_finished(event)

    assert _runs_value("job_err", "error") == before + 1


def test_executed_without_submitted_still_counts_run():
    # A listener attached mid-flight can see executed without submitted;
    # the run must count even though no duration can be observed.
    event = SimpleNamespace(job_id="job_orphan", exception=None)
    before = _runs_value("job_orphan", "ok")

    scheduler_metrics._on_finished(event)

    assert _runs_value("job_orphan", "ok") == before + 1
    assert _duration_count("job_orphan") == 0


def test_missed_clears_pending_start():
    event = SimpleNamespace(job_id="job_missed")
    scheduler_metrics._on_submitted(event)
    scheduler_metrics._on_missed(event)

    assert _runs_value("job_missed", "missed") == 1
    assert "job_missed" not in scheduler_metrics._started_at


def test_attach_registers_listeners():
    calls: list[tuple] = []
    fake_scheduler = SimpleNamespace(
        add_listener=lambda fn, mask: calls.append((fn, mask))
    )
    scheduler_metrics.attach_metrics(fake_scheduler)
    assert len(calls) == 3
