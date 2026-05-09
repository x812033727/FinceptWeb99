"""Tests for the ingest_health_history append + read path.

Covers two surfaces:
  1. `record_health` writes one row per call, with the correct
     outcome label derived via `_classify_outcome`.
  2. `get_health_history` aggregates by UTC day and returns the
     oldest-first list shape the frontend sparkline expects.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from models.ingest_health_history import IngestHealthHistory
from services.ingest import repository


@pytest.mark.asyncio
async def test_record_health_appends_history_row(db_session):
    """Each record_health call should land one IngestHealthHistory
    row with the outcome derived from (ok, error, silent_deny)."""
    await repository.record_health(
        "ingest_news_tw", ok=True, row_count=42,
    )
    await repository.record_health(
        "ingest_news_tw",
        ok=False,
        silent_deny="Your level is register, not sponsor",
    )
    await repository.record_health(
        "ingest_news_tw",
        ok=False,
        error="HTTP 503 from upstream",
    )

    rows = (
        await db_session.execute(
            select(IngestHealthHistory)
            .where(IngestHealthHistory.job_id == "ingest_news_tw")
            .order_by(IngestHealthHistory.id)
        )
    ).scalars().all()
    assert [r.outcome for r in rows] == ["ok", "silent_deny", "failed"]
    assert rows[0].row_count == 42


@pytest.mark.asyncio
async def test_get_health_history_aggregates_by_utc_day(db_session):
    """Three records on day-A and one on day-B should roll up into
    two day buckets, oldest-first."""
    base = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    day_a = base - timedelta(days=2)
    day_b = base - timedelta(days=1)
    db_session.add_all([
        IngestHealthHistory(
            job_id="ingest_revenue_tw", outcome="ok",
            row_count=100, recorded_at=day_a,
        ),
        IngestHealthHistory(
            job_id="ingest_revenue_tw", outcome="ok",
            row_count=120, recorded_at=day_a + timedelta(hours=1),
        ),
        IngestHealthHistory(
            job_id="ingest_revenue_tw", outcome="failed",
            row_count=0, recorded_at=day_a + timedelta(hours=2),
        ),
        IngestHealthHistory(
            job_id="ingest_revenue_tw", outcome="silent_deny",
            row_count=0, recorded_at=day_b,
        ),
    ])
    await db_session.commit()

    history = await repository.get_health_history("ingest_revenue_tw", days=7)
    by_date = {h.date: h for h in history}
    assert len(history) == 2

    a_iso = day_a.date().isoformat()
    b_iso = day_b.date().isoformat()
    assert by_date[a_iso].ok == 2
    assert by_date[a_iso].failed == 1
    assert by_date[a_iso].silent_deny == 0
    assert by_date[b_iso].silent_deny == 1
    assert by_date[b_iso].ok == 0
    # Oldest-first ordering — the frontend renders the strip
    # left-to-right in this order.
    assert history[0].date == a_iso
    assert history[1].date == b_iso


@pytest.mark.asyncio
async def test_get_health_history_clips_to_window(db_session):
    """A row 10 days old must NOT be returned for `days=7`."""
    base = datetime.now(UTC)
    db_session.add_all([
        IngestHealthHistory(
            job_id="ingest_old", outcome="ok",
            recorded_at=base - timedelta(days=10),
        ),
        IngestHealthHistory(
            job_id="ingest_old", outcome="ok",
            recorded_at=base - timedelta(days=2),
        ),
    ])
    await db_session.commit()

    history = await repository.get_health_history("ingest_old", days=7)
    assert len(history) == 1
    # The clipped row's date must be within the last 7 days.
    today = base.date()
    history_date = datetime.fromisoformat(history[0].date + "T00:00:00").date()
    assert (today - history_date).days <= 7


@pytest.mark.asyncio
async def test_get_health_history_returns_empty_for_unknown_job(db_session):
    """Unknown job_id returns [] rather than raising — the UI's
    sparkline column then renders all-gray cells without erroring."""
    history = await repository.get_health_history("ingest_does_not_exist")
    assert history == []


@pytest.mark.asyncio
async def test_get_health_history_returns_empty_for_zero_days(db_session):
    """`days=0` is treated as "no window" and short-circuits to [],
    bypassing the SQL query — defensive for callers that compute the
    window dynamically."""
    history = await repository.get_health_history("ingest_x", days=0)
    assert history == []
