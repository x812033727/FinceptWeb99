"""Admin /api/admin/ingest/health endpoint tests.

The repository's `list_health` is mocked so the test stays at the HTTP /
auth / serialization layer without touching Redis. Round-trip tests for
`record_health` / `get_health` themselves are deferred to Phase 4 once
ingest health gets a frontend panel that exercises the full path.
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from services.ingest.repository import IngestHealth
from tests.test_admin_api import _auth, _promote_to_admin, _register_login


@pytest.mark.asyncio
async def test_ingest_health_requires_admin(client: AsyncClient):
    token = await _register_login(client, "ingest_viewer@test.com")
    r = await client.get("/api/admin/ingest/health", headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_ingest_health_requires_auth(client: AsyncClient):
    r = await client.get("/api/admin/ingest/health")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ingest_health_returns_recorded_jobs(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "ingest_admin@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    canned = [
        IngestHealth(
            job_id="ingest_ohlcv_tw",
            last_run_at="2026-04-28T06:30:00+00:00",
            ok=True,
            row_count=42_000,
            error=None,
        ),
    ]
    with patch(
        "services.ingest.repository.list_health",
        AsyncMock(return_value=canned),
    ):
        r = await client.get("/api/admin/ingest/health", headers=_auth(token))

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["job_id"] == "ingest_ohlcv_tw"
    assert body[0]["ok"] is True
    assert body[0]["row_count"] == 42_000
    assert body[0]["error"] is None


@pytest.mark.asyncio
async def test_ingest_health_round_trips_silent_deny_and_freshness(
    client: AsyncClient, db_session: AsyncSession,
):
    """The new optional fields must survive serialization → HTTP → JSON."""
    email = "ingest_admin_extra@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    canned = [
        IngestHealth(
            job_id="ingest_revenue_tw",
            last_run_at="2026-04-28T09:00:00+00:00",
            ok=False,
            row_count=0,
            error="silent_paywall: Your level is register, not sponsor",
            silent_deny="Your level is register, not sponsor",
            latest_data_ts="2026-03-31",
        ),
    ]
    with patch(
        "services.ingest.repository.list_health",
        AsyncMock(return_value=canned),
    ):
        r = await client.get("/api/admin/ingest/health", headers=_auth(token))

    assert r.status_code == 200
    body = r.json()[0]
    assert body["silent_deny"] == "Your level is register, not sponsor"
    assert body["latest_data_ts"] == "2026-03-31"
    assert body["error"].startswith("silent_paywall")


@pytest.mark.asyncio
async def test_ingest_health_computes_data_stale_via_trading_days(
    client: AsyncClient, db_session: AsyncSession,
):
    """API-layer regression: the `data_stale` boolean must be derived
    from `is_data_stale` (trading-day-aware), not from a calendar-day
    check. Two rows in one query: one fresh (Mon run + prev-Fri data
    = 0 trading days behind), one stale (Fri run + prev Mon data = 3
    trading days behind)."""
    email = "ingest_admin_stale@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    canned = [
        IngestHealth(
            job_id="ingest_ohlcv_tw",
            last_run_at="2026-04-27T06:30:00+00:00",   # Mon morning
            ok=True,
            row_count=2000,
            error=None,
            latest_data_ts="2026-04-24",                # Fri's data
        ),
        IngestHealth(
            job_id="ingest_taiex_history",
            last_run_at="2026-05-01T07:10:00+00:00",   # Fri
            ok=True,
            row_count=1,
            error=None,
            latest_data_ts="2026-04-27",                # prev Mon
        ),
    ]
    with patch(
        "services.ingest.repository.list_health",
        AsyncMock(return_value=canned),
    ):
        r = await client.get("/api/admin/ingest/health", headers=_auth(token))

    assert r.status_code == 200
    body = {row["job_id"]: row for row in r.json()}
    # Mon run reading Fri data — weekend-aware = NOT stale
    assert body["ingest_ohlcv_tw"]["data_stale"] is False
    # Fri run with prev-Mon data — 3 trading days behind = stale
    assert body["ingest_taiex_history"]["data_stale"] is True


@pytest.mark.asyncio
async def test_ingest_history_endpoint_returns_aggregated_payload(
    client: AsyncClient, db_session: AsyncSession,
):
    """End-to-end: admin GET /ingest/{job_id}/history returns the
    daily aggregate produced by `repository.get_health_history`,
    sorted oldest-first, with `days` echoing the requested window."""
    from datetime import UTC, datetime, timedelta
    from models.ingest_health_history import IngestHealthHistory

    email = "ingest_history_admin@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    base = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    db_session.add_all([
        IngestHealthHistory(
            job_id="ingest_news_tw", outcome="ok",
            row_count=10, recorded_at=base - timedelta(days=2),
        ),
        IngestHealthHistory(
            job_id="ingest_news_tw", outcome="failed",
            recorded_at=base - timedelta(days=2, hours=1),
        ),
        IngestHealthHistory(
            job_id="ingest_news_tw", outcome="silent_deny",
            recorded_at=base - timedelta(days=1),
        ),
    ])
    await db_session.commit()

    r = await client.get(
        "/api/admin/ingest/ingest_news_tw/history",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "ingest_news_tw"
    assert body["days"] == 7
    assert len(body["history"]) == 2
    # Oldest first — frontend renders the strip in this order.
    day_a = (base - timedelta(days=2)).date().isoformat()
    day_b = (base - timedelta(days=1)).date().isoformat()
    assert body["history"][0]["date"] == day_a
    assert body["history"][0]["ok"] == 1
    assert body["history"][0]["failed"] == 1
    assert body["history"][1]["date"] == day_b
    assert body["history"][1]["silent_deny"] == 1


@pytest.mark.asyncio
async def test_ingest_history_endpoint_clamps_days_parameter(
    client: AsyncClient, db_session: AsyncSession,
):
    """`days` is clamped to [1, 30]. A request for days=999 should
    return days=30 in the response, not blow up the DB query."""
    email = "ingest_history_clamp@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    r = await client.get(
        "/api/admin/ingest/ingest_x/history?days=999",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["days"] == 30

    r = await client.get(
        "/api/admin/ingest/ingest_x/history?days=0",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["days"] == 1


@pytest.mark.asyncio
async def test_ingest_history_requires_admin(client: AsyncClient):
    """Non-admin users should not see ingest history payloads —
    `/api/admin/*` is consistently 403 for viewer + analyst roles."""
    token = await _register_login(client, "ingest_history_viewer@test.com")
    r = await client.get(
        "/api/admin/ingest/ingest_news_tw/history",
        headers=_auth(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_ingest_health_returns_empty_when_no_jobs(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "ingest_admin_empty@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    with patch("services.ingest.repository.list_health", AsyncMock(return_value=[])):
        r = await client.get("/api/admin/ingest/health", headers=_auth(token))

    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_ingest_retry_requires_admin(client: AsyncClient):
    token = await _register_login(client, "ingest_retry_viewer@test.com")
    r = await client.post(
        "/api/admin/ingest/ingest_news_tw/retry",
        headers=_auth(token),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_ingest_retry_unknown_job_404(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "ingest_retry_unknown_admin@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    r = await client.post(
        "/api/admin/ingest/not_a_real_job/retry",
        headers=_auth(token),
    )

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_ingest_retry_clears_backoff_and_queues_run(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "ingest_retry_admin@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    with patch(
        "services.ingest.repository.clear_failures",
        AsyncMock(),
    ) as clear_failures, patch(
        "services.ingest.repository.record_health",
        AsyncMock(),
    ) as record_health, patch(
        "api.admin.router._run_ingest_job_once",
        AsyncMock(),
    ) as retry_job:
        r = await client.post(
            "/api/admin/ingest/ingest_news_tw/retry",
            headers=_auth(token),
        )

    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    clear_failures.assert_awaited_once_with("ingest_news_tw")
    record_health.assert_awaited_once()
    retry_job.assert_awaited_once_with("ingest_news_tw")


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", [
    "ingest_news_tw",
    "ingest_news_international",
    "ingest_ohlcv_tw",
    "ingest_institutional_tw",
    "ingest_margin_tw",
    "ingest_revenue_tw",
    "ingest_taiex_history",
    "score_discussion_outcomes",
])
async def test_all_whitelisted_jobs_are_retryable(
    job_id: str, client: AsyncClient, db_session: AsyncSession,
):
    """Pin the cross-side contract: every entry in
    `RETRYABLE_INGEST_JOBS` must dispatch successfully via the
    retry endpoint. Catches the case where a new entry is added
    server-side but `_run_ingest_job_once` was forgotten — the
    button would render but POST 404 silently."""
    email = f"ingest_retry_param_{job_id}@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    with patch(
        "services.ingest.repository.clear_failures", AsyncMock(),
    ), patch(
        "services.ingest.repository.record_health", AsyncMock(),
    ), patch(
        "api.admin.router._run_ingest_job_once", AsyncMock(),
    ) as retry_job:
        r = await client.post(
            f"/api/admin/ingest/{job_id}/retry",
            headers=_auth(token),
        )

    assert r.status_code == 200, f"job {job_id!r} returned {r.status_code}"
    retry_job.assert_awaited_once_with(job_id)


@pytest.mark.asyncio
async def test_scheduler_health_requires_admin(client: AsyncClient):
    token = await _register_login(client, "scheduler_viewer@test.com")
    r = await client.get("/api/admin/scheduler/health", headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_scheduler_health_returns_heartbeat_snapshot(
    client: AsyncClient, db_session: AsyncSession,
):
    """End-to-end: admin hits /scheduler/health, gets the structured
    snapshot back. Shape mirrors `SchedulerHeartbeat` dataclass."""
    from services.scheduler_health import SchedulerHeartbeat

    email = "scheduler_admin@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    canned = SchedulerHeartbeat(
        last_beat_at="2026-05-09T12:00:00+00:00",
        age_seconds=15.0,
        stale=False,
        version="0.5.84",
        ttl_seconds=180,
    )
    with patch(
        "services.scheduler_health.read_heartbeat",
        AsyncMock(return_value=canned),
    ):
        r = await client.get(
            "/api/admin/scheduler/health", headers=_auth(token),
        )

    assert r.status_code == 200
    body = r.json()
    assert body["last_beat_at"] == "2026-05-09T12:00:00+00:00"
    assert body["age_seconds"] == 15.0
    assert body["stale"] is False
    assert body["version"] == "0.5.84"
    assert body["ttl_seconds"] == 180


@pytest.mark.asyncio
async def test_scheduler_health_surfaces_stale_when_missing(
    client: AsyncClient, db_session: AsyncSession,
):
    """When the heartbeat key is missing entirely, the endpoint still
    returns 200 (so the AdminPage doesn't error) — but `stale=True`
    + `last_beat_at=None` so the UI can render the dead-scheduler
    warning."""
    from services.scheduler_health import SchedulerHeartbeat

    email = "scheduler_admin_dead@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    canned = SchedulerHeartbeat(
        last_beat_at=None,
        age_seconds=None,
        stale=True,
        version=None,
        ttl_seconds=180,
    )
    with patch(
        "services.scheduler_health.read_heartbeat",
        AsyncMock(return_value=canned),
    ):
        r = await client.get(
            "/api/admin/scheduler/health", headers=_auth(token),
        )

    assert r.status_code == 200
    body = r.json()
    assert body["last_beat_at"] is None
    assert body["age_seconds"] is None
    assert body["stale"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", [
    "ingest_news_tw",
    "ingest_news_international",
    "ingest_ohlcv_tw",
    "ingest_institutional_tw",
    "ingest_margin_tw",
    "ingest_revenue_tw",
    "ingest_taiex_history",
    "score_discussion_outcomes",
])
async def test_run_ingest_job_once_dispatches_each_id(job_id: str):
    """The dispatcher itself must have a branch per whitelisted
    job. Mocks the underlying task module's `run` so we don't fire
    real HTTP / DB work — we just want to confirm the dispatcher
    found the right module."""
    from api.admin import router as admin_router

    module_path = {
        "ingest_news_tw": "tasks.ingest_news_tw",
        "ingest_news_international": "tasks.ingest_news_international",
        "ingest_ohlcv_tw": "tasks.ingest_ohlcv_tw",
        "ingest_institutional_tw": "tasks.ingest_institutional_tw",
        "ingest_margin_tw": "tasks.ingest_margin_tw",
        "ingest_revenue_tw": "tasks.ingest_revenue_tw",
        "ingest_taiex_history": "tasks.ingest_taiex_history",
        "score_discussion_outcomes": "tasks.score_discussion_outcomes",
    }[job_id]

    with patch(f"{module_path}.run", AsyncMock()) as task_run:
        await admin_router._run_ingest_job_once(job_id)

    task_run.assert_awaited_once()
