"""Tests for `/api/admin/finmind/*` — main-app admin proxy to the
FinMind clone.

The proxy crosses the architectural boundary documented in CLAUDE.md
(FinMind clone is normally consumed via HTTP, not in-process imports)
ONLY for the AdminPage so the React app uses its existing JWT admin
auth. This test file pins the contract.

Setup gymnastics: the FinMind subsystem owns its own DB engine. To
make the proxy work in the test env (which uses the main app's
in-memory SQLite), we override `get_finmind_db` to yield a dedicated
SQLite session built from `finmind.db.base.Base.metadata` only.
"""
from __future__ import annotations

import os

# Force in-memory SQLite for the FinMind subsystem BEFORE any of its
# modules are imported (matches `backend/finmind/tests/conftest.py`).
os.environ.setdefault(
    "FINMIND_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
)

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from models.user import User, UserRole


# ── Helpers (mirror test_admin_api.py) ──────────────────────────


async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post(
        "/api/auth/register",
        json={"email": email, "password": "Test1234!"},
    )
    r = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Test1234!"},
    )
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _promote_to_admin(
    db: AsyncSession, email: str, client: AsyncClient
) -> str:
    from sqlalchemy import select

    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one()
    user.role = UserRole.admin
    await db.commit()

    r = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Test1234!"},
    )
    return r.json()["access_token"]


# ── FinMind DB fixture ──────────────────────────────────────────


@pytest_asyncio.fixture
async def finmind_db_override():
    """Build an in-memory FinMind DB + override the proxy's
    `get_finmind_db` dep so it yields against this engine instead of
    the configured `FINMIND_DATABASE_URL` (which would try to talk to
    a real Postgres in CI)."""
    # Late imports — keep these inside the fixture so the test module
    # itself doesn't pull finmind.db.session at collection time
    # (which eagerly creates an asyncpg engine if the env var isn't
    # patched in time).
    import finmind.models  # noqa: F401  (registers all FinMind models)
    from finmind.db.base import Base as FinmindBase

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(FinmindBase.metadata.create_all)

    SessionLocal = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False,
    )

    async def _override():
        async with SessionLocal() as s:
            yield s

    from finmind.db.session import get_finmind_db
    from main import app

    app.dependency_overrides[get_finmind_db] = _override
    try:
        yield SessionLocal
    finally:
        app.dependency_overrides.pop(get_finmind_db, None)
        await engine.dispose()


async def _seed_catalog(SessionLocal) -> None:
    """Wrap finmind.scripts.init_db.seed_dataset_sources in a fresh
    session bound to our test engine (the script's own session
    factory is monkeypatched per-test in the finmind subsystem's
    conftest, but here we're in main backend test land)."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from data.tw.finmind_datasets import find_dataset
    from finmind.dataset_catalog import all_entries
    from finmind.models.dataset_source import DatasetSource

    rows = []
    for category, entry in all_entries():
        spec = find_dataset(entry.dataset_code)
        rows.append({
            "dataset_code": entry.dataset_code,
            "category": category,
            "description_zh": spec.description,
            "local_table": entry.local_table,
            "per_symbol": spec.per_symbol,
            "primary_source": entry.primary_source,
            "fallback_source": entry.fallback_source,
            "active_source": entry.primary_source,
            "sponsor_tier": spec.sponsor_tier,
            "ingest_freq": entry.ingest_freq,
        })

    async with SessionLocal() as s:
        stmt = sqlite_insert(DatasetSource).values(rows)
        await s.execute(stmt)
        await s.commit()


# ── Access control ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_requires_auth(client, finmind_db_override):
    r = await client.get("/api/admin/finmind/datasets")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_proxy_rejects_non_admin(client, finmind_db_override):
    token = await _register_login(client, "viewer_finmind@test.com")
    r = await client.get(
        "/api/admin/finmind/datasets", headers=_auth(token),
    )
    assert r.status_code == 403


# ── Catalog list + PATCH ────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_lists_catalog_for_admin(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_list@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/datasets", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 80
    by_code = {row["dataset_code"]: row for row in body}
    assert "TaiwanStockPrice" in by_code
    assert by_code["TaiwanStockPrice"]["per_symbol"] is True


@pytest.mark.asyncio
async def test_proxy_patch_toggles_enabled(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_toggle@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanStockPrice",
        json={"enabled": True},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True


@pytest.mark.asyncio
async def test_proxy_patch_validates_active_source(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_valid@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanStockPrice",
        json={"active_source": "definitely_not_real"},
        headers=_auth(token),
    )
    assert r.status_code == 400
    assert "active_source must be" in r.json()["detail"]


@pytest.mark.asyncio
async def test_proxy_patch_phase_a_to_b_switch(
    client, db_session, finmind_db_override,
):
    """Headline operator action — flip Phase A → B for one dataset
    via a single PATCH. After the switch the read API metadata
    reflects the new active_source."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_switch@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.patch(
        "/api/admin/finmind/datasets/TaiwanStockPrice",
        json={"active_source": "twse"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["active_source"] == "twse"


# ── Status + usage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_status_returns_collect_status_shape(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_status@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/status", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    for k in (
        "alembic", "catalog", "phase1_coverage",
        "active_ingestion", "backfill", "recent_errors", "generated_at",
    ):
        assert k in body
    assert body["catalog"]["seeded"] == 80


@pytest.mark.asyncio
async def test_proxy_usage_empty_when_no_events(
    client, db_session, finmind_db_override,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_usage@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/usage?days=7", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7
    assert body["by_day"] == []
    assert body["by_dataset"] == []


@pytest.mark.asyncio
async def test_proxy_usage_aggregates_recent_events(
    client, db_session, finmind_db_override,
):
    """Insert a few api_usage_events rows with varied datasets +
    timestamps, verify the rollup groups them correctly. This is the
    happy path the FinmindUsageCard chart consumes."""
    from datetime import datetime, timezone

    from finmind.models.billing import ApiUsageEvent

    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    now = datetime.now(tz=timezone.utc)
    async with SessionLocal() as s:
        s.add_all([
            ApiUsageEvent(
                ts=now,
                api_key_id=1,
                dataset_code="TaiwanStockPrice",
                endpoint="/data/TaiwanStockPrice",
                row_count=100, bytes_out=1024, latency_ms=10,
                status_code=200,
            ),
            ApiUsageEvent(
                ts=now,
                api_key_id=2,
                dataset_code="TaiwanStockPrice",
                endpoint="/data/TaiwanStockPrice",
                row_count=50, bytes_out=512, latency_ms=8,
                status_code=200,
            ),
            ApiUsageEvent(
                ts=now,
                api_key_id=1,
                dataset_code="TaiwanStockMarginPurchaseShortSale",
                endpoint="/data/TaiwanStockMarginPurchaseShortSale",
                row_count=30, bytes_out=256, latency_ms=5,
                status_code=200,
            ),
        ])
        await s.commit()

    email = "admin_fm_usage_pop@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/usage?days=7", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7

    # by_day: one entry for today with calls=3, rows=180
    assert len(body["by_day"]) == 1
    assert body["by_day"][0]["calls"] == 3
    assert body["by_day"][0]["rows"] == 180

    # by_dataset: TaiwanStockPrice should top the list (2 calls, 150 rows)
    by_dataset = {d["dataset_code"]: d for d in body["by_dataset"]}
    assert by_dataset["TaiwanStockPrice"]["calls"] == 2
    assert by_dataset["TaiwanStockPrice"]["rows"] == 150
    assert by_dataset["TaiwanStockMarginPurchaseShortSale"]["calls"] == 1


@pytest.mark.asyncio
async def test_proxy_usage_rejects_invalid_days(
    client, db_session, finmind_db_override,
):
    email = "admin_fm_usage_v@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/usage?days=999", headers=_auth(token),
    )
    assert r.status_code == 400
    assert "between 1 and 90" in r.json()["detail"]


# ── Manual run buttons ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_dataset_returns_404_for_unknown(
    client, db_session, finmind_db_override,
):
    email = "admin_fm_run404@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/TotallyMadeUp/run",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_dataset_returns_503_when_no_local_table(
    client, db_session, finmind_db_override,
):
    """Realtime / unbuilt datasets have empty local_table — trigger
    must NOT proceed (would UPSERT into ''). Surface 503 cleanly."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_run503@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/taiwan_stock_tick_snapshot/run",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 503
    assert "destination table not yet built" in r.json()["detail"]


@pytest.mark.asyncio
async def test_run_dataset_default_window_is_last_7_days(
    client, db_session, finmind_db_override, monkeypatch,
):
    """Without start/end the trigger backfills the last 7 days —
    matches the cron's default window. Verify the runner sees those
    bounds by capturing the call args."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    captured: dict = {}

    async def fake_ingest_chunk(session, **kwargs):
        captured.update(kwargs)
        from finmind.ingest.runner import ChunkResult
        return ChunkResult(status="done", rows_written=5, error=None)

    import finmind.ingest.runner as _runner
    monkeypatch.setattr(_runner, "ingest_chunk", fake_ingest_chunk)

    email = "admin_fm_run_def@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/TaiwanStockPrice/run",
        json={},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["rows_written"] == 5

    # Default window: 7 days back from today.
    from datetime import date, timedelta
    assert captured["range_end"] == date.today()
    assert captured["range_start"] == date.today() - timedelta(days=7)


@pytest.mark.asyncio
async def test_run_dataset_honors_explicit_dates_and_symbol(
    client, db_session, finmind_db_override, monkeypatch,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    captured: dict = {}

    async def fake_ingest_chunk(session, **kwargs):
        captured.update(kwargs)
        from finmind.ingest.runner import ChunkResult
        return ChunkResult(status="done", rows_written=42, error=None)

    import finmind.ingest.runner as _runner
    monkeypatch.setattr(_runner, "ingest_chunk", fake_ingest_chunk)

    email = "admin_fm_run_explicit@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/datasets/TaiwanStockPrice/run",
        json={
            "symbol": "2330",
            "start_date": "2024-03-01",
            "end_date": "2024-03-31",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "2330"
    assert body["range_start"] == "2024-03-01"
    assert body["range_end"] == "2024-03-31"

    from datetime import date
    assert captured["symbol"] == "2330"
    assert captured["range_start"] == date(2024, 3, 1)
    assert captured["range_end"] == date(2024, 3, 31)


@pytest.mark.asyncio
async def test_run_due_returns_summary_when_nothing_enabled(
    client, db_session, finmind_db_override,
):
    """Default catalog state — no datasets enabled → run_due_now
    returns 0 outcomes. Endpoint must still 200 with empty summary
    so the frontend renders "0 chunks" not an error."""
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    email = "admin_fm_rd_empty@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/run-due", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["done"] == 0
    assert body["failed"] == 0
    assert body["outcomes"] == []


# ── API key issuance ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_key_returns_plaintext_once(
    client, db_session, finmind_db_override,
):
    """Plaintext is exposed in the POST response and never readable
    again (we only store sha256). Assert the response shape + that
    the underlying issue_key actually persisted a row."""
    SessionLocal = finmind_db_override

    email = "admin_fm_keys_issue@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "customer@example.com", "name": "prod"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plaintext"].startswith("fck_live_")
    assert body["prefix"].startswith("fck_live_")
    assert body["owner_email"] == "customer@example.com"
    assert body["record_id"] > 0

    # Verify row landed in api_keys.
    from finmind.models.billing import ApiKey

    async with SessionLocal() as s:
        row = await s.get(ApiKey, body["record_id"])
        assert row is not None
        assert row.owner_email == "customer@example.com"
        assert row.name == "prod"
        assert row.enabled is True


@pytest.mark.asyncio
async def test_list_keys_omits_secrets(
    client, db_session, finmind_db_override,
):
    """The listing endpoint must NEVER include `plaintext` or
    `key_hash` — operator UI shows only prefix + metadata."""
    email = "admin_fm_keys_list@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    # Issue two keys first.
    for who in ("a@x.com", "b@x.com"):
        await client.post(
            "/api/admin/finmind/keys",
            json={"owner_email": who},
            headers=_auth(token),
        )

    r = await client.get(
        "/api/admin/finmind/keys", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    for item in body:
        assert "plaintext" not in item
        assert "key_hash" not in item
        assert "prefix" in item
        assert item["enabled"] is True


@pytest.mark.asyncio
async def test_revoke_key_soft_deletes(
    client, db_session, finmind_db_override,
):
    """DELETE flips enabled=false rather than removing the row —
    keeps audit trail + api_usage_events FK references valid."""
    SessionLocal = finmind_db_override

    email = "admin_fm_keys_rev@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    issued = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "to-revoke@example.com"},
        headers=_auth(token),
    )
    record_id = issued.json()["record_id"]

    r = await client.delete(
        f"/api/admin/finmind/keys/{record_id}", headers=_auth(token),
    )
    assert r.status_code == 204

    # Row still exists; just disabled.
    from finmind.models.billing import ApiKey

    async with SessionLocal() as s:
        row = await s.get(ApiKey, record_id)
        assert row is not None
        assert row.enabled is False

    # Listing still shows it.
    listing = await client.get(
        "/api/admin/finmind/keys", headers=_auth(token),
    )
    revoked = next(k for k in listing.json() if k["id"] == record_id)
    assert revoked["enabled"] is False


@pytest.mark.asyncio
async def test_revoke_key_404_for_unknown(
    client, db_session, finmind_db_override,
):
    email = "admin_fm_keys_404@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.delete(
        "/api/admin/finmind/keys/999999", headers=_auth(token),
    )
    assert r.status_code == 404


# ── Plans CRUD ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_plans_empty(
    client, db_session, finmind_db_override,
):
    email = "admin_plans_empty@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/finmind/plans", headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_upsert_plan_creates_then_updates(
    client, db_session, finmind_db_override,
):
    """Same PUT endpoint creates the plan first time, updates the
    second — idempotent contract the frontend relies on."""
    email = "admin_plans_upsert@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.put(
        "/api/admin/finmind/plans/pro",
        json={
            "name": "Pro",
            "price_monthly": 990,
            "quota_daily_calls": 10_000,
            "quota_daily_rows": 1_000_000,
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["code"] == "pro"
    assert r.json()["quota_daily_calls"] == 10_000

    r = await client.put(
        "/api/admin/finmind/plans/pro",
        json={
            "name": "Pro (updated)",
            "price_monthly": 1990,
            "quota_daily_calls": 20_000,
            "quota_daily_rows": 2_000_000,
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Pro (updated)"
    assert r.json()["quota_daily_calls"] == 20_000

    listing = await client.get(
        "/api/admin/finmind/plans", headers=_auth(token),
    )
    assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_disable_plan_soft_deletes(
    client, db_session, finmind_db_override,
):
    """DELETE flips enabled=false rather than removing the row —
    keeps subscriptions FK-valid."""
    email = "admin_plans_disable@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    await client.put(
        "/api/admin/finmind/plans/lite",
        json={"name": "Lite", "quota_daily_calls": 500, "quota_daily_rows": 50_000},
        headers=_auth(token),
    )
    r = await client.delete(
        "/api/admin/finmind/plans/lite", headers=_auth(token),
    )
    assert r.status_code == 204

    listing = await client.get(
        "/api/admin/finmind/plans", headers=_auth(token),
    )
    plan = next(p for p in listing.json() if p["code"] == "lite")
    assert plan["enabled"] is False


@pytest.mark.asyncio
async def test_disable_plan_404_for_unknown(
    client, db_session, finmind_db_override,
):
    email = "admin_plans_404@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.delete(
        "/api/admin/finmind/plans/nope", headers=_auth(token),
    )
    assert r.status_code == 404


# ── Key-to-plan linking ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_issue_key_with_plan_creates_subscription(
    client, db_session, finmind_db_override,
):
    """Headline integration: POST /keys with plan_code → backend
    creates an active Subscription and links the new ApiKey to it."""
    SessionLocal = finmind_db_override

    email = "admin_keys_plan@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    await client.put(
        "/api/admin/finmind/plans/pro",
        json={
            "name": "Pro",
            "price_monthly": 990,
            "quota_daily_calls": 10_000,
            "quota_daily_rows": 1_000_000,
        },
        headers=_auth(token),
    )

    r = await client.post(
        "/api/admin/finmind/keys",
        json={
            "owner_email": "customer@example.com",
            "plan_code": "pro",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["plan_code"] == "pro"
    assert body["subscription_id"] is not None

    listing = await client.get(
        "/api/admin/finmind/keys", headers=_auth(token),
    )
    key = next(k for k in listing.json() if k["id"] == body["record_id"])
    assert key["plan_code"] == "pro"
    assert key["subscription_id"] == body["subscription_id"]

    from finmind.models.billing import Subscription
    async with SessionLocal() as s:
        sub = await s.get(Subscription, body["subscription_id"])
        assert sub is not None
        assert sub.owner_email == "customer@example.com"
        assert sub.plan_code == "pro"
        assert sub.status == "active"


@pytest.mark.asyncio
async def test_issue_key_with_unknown_plan_falls_back_to_free_tier(
    client, db_session, finmind_db_override,
):
    """Unknown plan_code shouldn't 4xx — operator notices the missing
    link in the keys table and re-links explicitly."""
    email = "admin_keys_unknownplan@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/keys",
        json={
            "owner_email": "customer@example.com",
            "plan_code": "definitely_not_a_plan",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["plan_code"] is None
    assert r.json()["subscription_id"] is None


@pytest.mark.asyncio
async def test_issue_key_with_disabled_plan_falls_back_to_free_tier(
    client, db_session, finmind_db_override,
):
    """Plan exists but enabled=false — same fallback as unknown."""
    email = "admin_keys_disabled@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    await client.put(
        "/api/admin/finmind/plans/sunset",
        json={"name": "Sunset", "quota_daily_calls": 100, "quota_daily_rows": 1_000},
        headers=_auth(token),
    )
    await client.delete(
        "/api/admin/finmind/plans/sunset", headers=_auth(token),
    )

    r = await client.post(
        "/api/admin/finmind/keys",
        json={"owner_email": "x@x.com", "plan_code": "sunset"},
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["plan_code"] is None


@pytest.mark.asyncio
async def test_run_due_invokes_runner_for_enabled_datasets(
    client, db_session, finmind_db_override, monkeypatch,
):
    SessionLocal = finmind_db_override
    await _seed_catalog(SessionLocal)

    # Enable one dataset so run_due_now has work.
    from finmind.models.dataset_source import DatasetSource as DS
    async with SessionLocal() as s:
        row = await s.get(DS, "TaiwanStockTotalMarginPurchaseShortSale")
        row.enabled = True
        await s.commit()

    # Mock ingest_chunk so we don't actually call FinMind.
    async def fake_ingest_chunk(session, **kwargs):
        from finmind.ingest.runner import ChunkResult
        return ChunkResult(status="done", rows_written=3, error=None)

    import finmind.ingest.runner as _runner
    monkeypatch.setattr(_runner, "ingest_chunk", fake_ingest_chunk)

    email = "admin_fm_rd_run@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.post(
        "/api/admin/finmind/run-due", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["done"] == 1
    assert body["rows_written"] == 3
    assert body["outcomes"][0]["dataset_code"] == "TaiwanStockTotalMarginPurchaseShortSale"
