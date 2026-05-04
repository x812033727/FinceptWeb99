"""Tests for `finmind.billing.quota` + `/api/finmind/data` quota gate.

Covers:
  - record_usage appends per-request rows
  - daily counter only sums today + status_code < 400 (failures don't
    burn quota)
  - check_quota returns plan limits + remaining for header rendering
  - Free-tier defaults when subscription / plan absent
  - 200 path: data endpoint records usage with row_count + bytes_out
  - 429 path: data endpoint blocks once over the call budget, sets
    Retry-After header, and the failed-quota request itself is logged
    so admins can see who's hitting the wall
  - dev-mode + env-allowlist auth paths skip quota entirely
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from finmind.api.router import router
from finmind.billing.keys import issue_key
from finmind.billing.quota import (
    _FREE_TIER_CALL_LIMIT,
    _FREE_TIER_ROW_LIMIT,
    check_quota,
    current_daily_usage,
    record_usage,
)
from finmind.db.session import get_finmind_db
from finmind.models.billing import ApiKey, ApiUsageEvent, Plan, Subscription
from finmind.scripts.init_db import seed_dataset_sources


# ── Pure quota counter ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_appends_one_row(finmind_session):
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    await record_usage(
        finmind_session,
        api_key_id=issued.record_id,
        dataset_code="TaiwanStockPrice",
        endpoint="/data/TaiwanStockPrice",
        row_count=42,
        bytes_out=2048,
        latency_ms=15,
        status_code=200,
    )

    n = (
        await finmind_session.execute(
            select(func.count()).select_from(ApiUsageEvent)
        )
    ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_daily_usage_sums_calls_and_rows_today(finmind_session):
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    for n_rows in (10, 25, 100):
        await record_usage(
            finmind_session,
            api_key_id=issued.record_id,
            dataset_code="TaiwanStockPrice",
            endpoint="/data/TaiwanStockPrice",
            row_count=n_rows,
            bytes_out=n_rows * 100,
            latency_ms=10,
            status_code=200,
        )

    usage = await current_daily_usage(finmind_session, issued.record_id)
    assert usage.calls == 3
    assert usage.rows == 135  # 10+25+100


@pytest.mark.asyncio
async def test_daily_usage_excludes_failed_requests(finmind_session):
    """A 4xx / 5xx response shouldn't burn the customer's quota — a
    systemic outage on our side mustn't cost them their daily allowance."""
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    await record_usage(
        finmind_session, api_key_id=issued.record_id,
        dataset_code="X", endpoint="/data/X",
        row_count=10, bytes_out=100, latency_ms=5, status_code=200,
    )
    await record_usage(
        finmind_session, api_key_id=issued.record_id,
        dataset_code="X", endpoint="/data/X",
        row_count=0, bytes_out=0, latency_ms=5, status_code=503,
    )
    await record_usage(
        finmind_session, api_key_id=issued.record_id,
        dataset_code="X", endpoint="/data/X",
        row_count=0, bytes_out=0, latency_ms=5, status_code=429,
    )

    usage = await current_daily_usage(finmind_session, issued.record_id)
    assert usage.calls == 1  # only the 200 counts
    assert usage.rows == 10


@pytest.mark.asyncio
async def test_daily_usage_excludes_yesterday(finmind_session):
    """Counter resets at UTC midnight — yesterday's burn doesn't carry
    over. Backdate one event to yesterday by rewriting `ts` directly."""
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    await record_usage(
        finmind_session, api_key_id=issued.record_id,
        dataset_code="X", endpoint="/data/X",
        row_count=999, bytes_out=0, latency_ms=5, status_code=200,
    )
    # Backdate that row to 2 days ago.
    await finmind_session.execute(
        text(
            "UPDATE api_usage_events SET ts = :ts "
            "WHERE api_key_id = :kid"
        ),
        {
            "ts": datetime.now(tz=UTC) - timedelta(days=2),
            "kid": issued.record_id,
        },
    )
    await finmind_session.commit()

    usage = await current_daily_usage(finmind_session, issued.record_id)
    assert usage.calls == 0
    assert usage.rows == 0


@pytest.mark.asyncio
async def test_check_quota_uses_free_tier_when_no_subscription(
    finmind_session,
):
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )
    api_key = await finmind_session.get(ApiKey, issued.record_id)

    status = await check_quota(finmind_session, api_key)
    assert status.permitted is True
    assert status.daily_call_limit == _FREE_TIER_CALL_LIMIT
    assert status.daily_row_limit == _FREE_TIER_ROW_LIMIT
    assert status.calls_used == 0
    assert status.calls_remaining == _FREE_TIER_CALL_LIMIT
    assert status.plan_code is None


@pytest.mark.asyncio
async def test_check_quota_uses_plan_limits_when_subscribed(
    finmind_session,
):
    """Issued key tied to a subscription on a plan with custom limits —
    check_quota must read those, not the free-tier defaults."""
    finmind_session.add(
        Plan(
            code="pro",
            name="Pro",
            quota_daily_calls=10_000,
            quota_daily_rows=1_000_000,
            currency="TWD",
            enabled=True,
        )
    )
    sub = Subscription(
        owner_email="user@example.com",
        plan_code="pro",
        status="active",
        started_at=date(2024, 1, 1),
    )
    finmind_session.add(sub)
    await finmind_session.commit()

    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
        subscription_id=sub.id,
    )
    api_key = await finmind_session.get(ApiKey, issued.record_id)

    status = await check_quota(finmind_session, api_key)
    assert status.daily_call_limit == 10_000
    assert status.daily_row_limit == 1_000_000
    assert status.plan_code == "pro"


@pytest.mark.asyncio
async def test_check_quota_blocks_when_call_limit_reached(finmind_session):
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )
    api_key = await finmind_session.get(ApiKey, issued.record_id)

    # Manually pre-fill the counter.
    for _ in range(_FREE_TIER_CALL_LIMIT):
        await record_usage(
            finmind_session, api_key_id=issued.record_id,
            dataset_code="X", endpoint="/data/X",
            row_count=1, bytes_out=10, latency_ms=1, status_code=200,
        )

    status = await check_quota(finmind_session, api_key)
    assert status.permitted is False
    assert "call quota exhausted" in (status.reason or "")


@pytest.mark.asyncio
async def test_check_quota_blocks_when_row_limit_reached(finmind_session):
    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )
    api_key = await finmind_session.get(ApiKey, issued.record_id)

    await record_usage(
        finmind_session, api_key_id=issued.record_id,
        dataset_code="X", endpoint="/data/X",
        row_count=_FREE_TIER_ROW_LIMIT, bytes_out=0, latency_ms=1,
        status_code=200,
    )

    status = await check_quota(finmind_session, api_key)
    assert status.permitted is False
    assert "row quota exhausted" in (status.reason or "")


# ── End-to-end via FastAPI TestClient ───────────────────────────


@pytest.fixture(autouse=True)
def _enable_dev_auth_bypass(monkeypatch):
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.delenv("FINMIND_API_KEYS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FINMIND_ADMIN_API_KEY", raising=False)


@pytest.fixture
def app(finmind_session):
    fastapi_app = FastAPI()
    fastapi_app.include_router(router, prefix="/api/finmind")

    async def _override():
        yield finmind_session

    fastapi_app.dependency_overrides[get_finmind_db] = _override
    return fastapi_app


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


async def _seed_one_ohlcv_row(finmind_session) -> None:
    await finmind_session.execute(
        text(
            "INSERT INTO ohlcv_daily "
            "(market, symbol, ts, open, high, low, close, volume, source) VALUES "
            "('TWSE', '2330', '2024-01-02', 600, 605, 598, 603, 12345, 'finmind')"
        )
    )
    await finmind_session.commit()


@pytest.mark.asyncio
async def test_data_endpoint_records_usage_for_real_key(
    client, finmind_session,
):
    """200 path with a real `fck_live_` key writes one usage row with
    the response's row_count + bytes_out + status=200."""
    await seed_dataset_sources()
    await _seed_one_ohlcv_row(finmind_session)

    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330"},
        headers={"X-Finmind-API-Key": issued.plaintext},
    )
    assert resp.status_code == 200

    # X-RateLimit-* headers populated for the customer.
    assert resp.headers["X-RateLimit-Limit"] == str(_FREE_TIER_CALL_LIMIT)
    assert int(resp.headers["X-RateLimit-Remaining"]) == _FREE_TIER_CALL_LIMIT - 1

    # Usage event recorded.
    events = (
        await finmind_session.execute(
            select(ApiUsageEvent).where(
                ApiUsageEvent.api_key_id == issued.record_id
            )
        )
    ).scalars().all()
    assert len(events) == 1
    e = events[0]
    assert e.dataset_code == "TaiwanStockPrice"
    assert e.endpoint == "/data/TaiwanStockPrice"
    assert e.row_count == 1
    assert e.status_code == 200
    assert e.bytes_out > 0


@pytest.mark.asyncio
async def test_data_endpoint_skips_quota_in_dev_mode(
    client, finmind_session,
):
    """DEBUG=true + empty allowlist (the dev-open path) bypasses the
    quota check entirely — no rate-limit headers, no 429 risk. The
    request IS still logged (with api_key_id=NULL) for analytics so
    operators can see anonymous traffic patterns; only the *quota
    enforcement* is skipped."""
    await seed_dataset_sources()
    await _seed_one_ohlcv_row(finmind_session)

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330"},
    )
    assert resp.status_code == 200
    assert "X-RateLimit-Limit" not in resp.headers

    # One anonymous (api_key_id=NULL) usage event landed.
    events = (
        await finmind_session.execute(select(ApiUsageEvent))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].api_key_id is None
    assert events[0].status_code == 200


@pytest.mark.asyncio
async def test_data_endpoint_returns_429_when_quota_exhausted(
    client, finmind_session,
):
    """Pre-burn the call quota, then verify the next request 429s with
    Retry-After + the over-quota row itself logged."""
    await seed_dataset_sources()
    await _seed_one_ohlcv_row(finmind_session)

    issued = await issue_key(
        finmind_session, owner_email="user@example.com",
    )

    for _ in range(_FREE_TIER_CALL_LIMIT):
        await record_usage(
            finmind_session, api_key_id=issued.record_id,
            dataset_code="TaiwanStockPrice",
            endpoint="/data/TaiwanStockPrice",
            row_count=1, bytes_out=10, latency_ms=1, status_code=200,
        )

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330"},
        headers={"X-Finmind-API-Key": issued.plaintext},
    )
    assert resp.status_code == 429
    assert "call quota exhausted" in resp.json()["detail"]
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0

    # The 429 itself is logged so admins can see "this customer is
    # hitting the wall" — but with status_code=429 it doesn't count
    # toward tomorrow's quota (test_daily_usage_excludes_failed_requests).
    rejected = (
        await finmind_session.execute(
            select(ApiUsageEvent).where(
                ApiUsageEvent.api_key_id == issued.record_id,
                ApiUsageEvent.status_code == 429,
            )
        )
    ).scalars().all()
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_data_endpoint_rejects_invalid_fck_live_key(
    client, finmind_session,
):
    """A `fck_live_` prefix that doesn't match any real key must 401
    rather than falling through to the env-var allowlist — surfaces
    tampered/expired keys loudly."""
    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330"},
        headers={"X-Finmind-API-Key": "fck_live_definitely_not_real_garbage"},
    )
    assert resp.status_code == 401
    assert "invalid or expired" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_data_endpoint_allowlist_path_skips_quota(
    finmind_session, monkeypatch, app,
):
    """Env-var allowlist keys are operator/dev keys — quota doesn't
    apply, no usage recorded."""
    await seed_dataset_sources()
    await _seed_one_ohlcv_row(finmind_session)

    monkeypatch.setenv("FINMIND_API_KEYS_ALLOWLIST", "operator-key-1")
    monkeypatch.setenv("DEBUG", "false")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as ac:
        resp = await ac.get(
            "/api/finmind/data/TaiwanStockPrice",
            params={"data_id": "2330"},
            headers={"X-Finmind-API-Key": "operator-key-1"},
        )
    assert resp.status_code == 200
    assert "X-RateLimit-Limit" not in resp.headers

    # Allowlist requests are also logged with api_key_id=NULL (no
    # api_keys row to attach), so analytics can still see them.
    events = (
        await finmind_session.execute(select(ApiUsageEvent))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].api_key_id is None
