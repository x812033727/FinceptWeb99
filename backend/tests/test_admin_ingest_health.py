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
@pytest.mark.parametrize("job_id", [
    "ingest_news_tw",
    "ingest_news_international",
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
        "ingest_institutional_tw": "tasks.ingest_institutional_tw",
        "ingest_margin_tw": "tasks.ingest_margin_tw",
        "ingest_revenue_tw": "tasks.ingest_revenue_tw",
        "ingest_taiex_history": "tasks.ingest_taiex_history",
        "score_discussion_outcomes": "tasks.score_discussion_outcomes",
    }[job_id]

    with patch(f"{module_path}.run", AsyncMock()) as task_run:
        await admin_router._run_ingest_job_once(job_id)

    task_run.assert_awaited_once()
