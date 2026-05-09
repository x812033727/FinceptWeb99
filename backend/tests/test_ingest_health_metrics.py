"""Verify `record_health` emits the four ingest Prometheus collectors
correctly for every (ok, error, silent_deny) combination it sees.

Tests rely on the in-process `prometheus_client` registry. Each test
captures `_value.get()` before and after the call and asserts the
delta — robust against parallel test pollution and cross-test ordering
because we never inspect absolute values.
"""
from datetime import UTC, date, datetime, timedelta

import pytest

from middleware.metrics import (
    INGEST_DATA_FRESHNESS_SECONDS,
    INGEST_ROWS_WRITTEN_TOTAL,
    INGEST_RUNS_TOTAL,
    INGEST_SILENT_DENY_TOTAL,
)
from services.ingest.repository import _classify_outcome, record_health


def _counter(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def _gauge(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def test_classify_outcome_maps_states_correctly():
    assert _classify_outcome(ok=True, error=None, silent_deny=None) == "ok"
    assert _classify_outcome(
        ok=False, error="silent_paywall: x", silent_deny="x",
    ) == "silent_deny"
    assert _classify_outcome(
        ok=False, error="skipped (backoff ...)", silent_deny=None,
    ) == "skipped"
    assert _classify_outcome(
        ok=False, error="queued: manual retry", silent_deny=None,
    ) == "skipped"
    assert _classify_outcome(
        ok=False, error="HTTP 503", silent_deny=None,
    ) == "failed"
    assert _classify_outcome(
        ok=False, error=None, silent_deny=None,
    ) == "failed"


@pytest.mark.asyncio
async def test_record_health_ok_increments_runs_and_rows():
    job = "test_job_metrics_ok"
    runs_before = _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="ok")
    rows_before = _counter(INGEST_ROWS_WRITTEN_TOTAL, job_id=job)

    await record_health(job, ok=True, row_count=42)

    assert _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="ok") == runs_before + 1
    assert _counter(INGEST_ROWS_WRITTEN_TOTAL, job_id=job) == rows_before + 42


@pytest.mark.asyncio
async def test_record_health_silent_deny_increments_dedicated_counter():
    job = "test_job_metrics_silent"
    runs_before = _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="silent_deny")
    deny_before = _counter(INGEST_SILENT_DENY_TOTAL, job_id=job, source="finmind")

    await record_health(
        job, ok=False, row_count=0,
        error="silent_paywall: tier-unavailable",
        silent_deny="tier-unavailable",
        source="finmind",
    )

    assert _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="silent_deny") == runs_before + 1
    assert _counter(INGEST_SILENT_DENY_TOTAL, job_id=job, source="finmind") == deny_before + 1


@pytest.mark.asyncio
async def test_record_health_skipped_classified_as_skipped():
    job = "test_job_metrics_skipped"
    skip_before = _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="skipped")

    await record_health(
        job, ok=False, row_count=0,
        error="skipped (backoff after 2 failures, ~119 min remaining)",
    )

    assert _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="skipped") == skip_before + 1


@pytest.mark.asyncio
async def test_record_health_failed_classified_as_failed():
    job = "test_job_metrics_failed"
    fail_before = _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="failed")

    await record_health(job, ok=False, row_count=0, error="HTTP 503 upstream")

    assert _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="failed") == fail_before + 1


@pytest.mark.asyncio
async def test_record_health_freshness_gauge_set_for_date():
    job = "test_job_metrics_freshness_date"
    yesterday = date.today() - timedelta(days=1)

    await record_health(job, ok=True, row_count=1, latest_data_ts=yesterday)

    age = _gauge(INGEST_DATA_FRESHNESS_SECONDS, job_id=job)
    # End-of-day UTC for `yesterday` means age is between 0 and ~48h.
    assert 0 <= age <= 2 * 24 * 3600


@pytest.mark.asyncio
async def test_record_health_freshness_gauge_set_for_datetime():
    job = "test_job_metrics_freshness_dt"
    five_min_ago = datetime.now(UTC) - timedelta(minutes=5)

    await record_health(job, ok=True, row_count=1, latest_data_ts=five_min_ago)

    age = _gauge(INGEST_DATA_FRESHNESS_SECONDS, job_id=job)
    assert 290 <= age <= 320


@pytest.mark.asyncio
async def test_record_health_no_row_increment_when_zero():
    """row_count=0 on a successful run (legit empty day) must NOT bump
    the rows counter — otherwise weekend / holiday days inflate it."""
    job = "test_job_metrics_zero_rows"
    rows_before = _counter(INGEST_ROWS_WRITTEN_TOTAL, job_id=job)

    await record_health(job, ok=True, row_count=0)

    assert _counter(INGEST_ROWS_WRITTEN_TOTAL, job_id=job) == rows_before


@pytest.mark.asyncio
async def test_record_health_backwards_compatible_signature():
    """Existing callers pass only positional + (ok, row_count, error).
    The new optional kwargs must not break them."""
    job = "test_job_metrics_compat"

    # No silent_deny / latest_data_ts / source — current call shape.
    await record_health(job, ok=True, row_count=5)
    await record_health(job, ok=False, error="boom")

    # Both classifications visible without exception.
    assert _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="ok") >= 1
    assert _counter(INGEST_RUNS_TOTAL, job_id=job, outcome="failed") >= 1
