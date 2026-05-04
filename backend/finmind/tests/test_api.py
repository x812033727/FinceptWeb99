"""Tests for `finmind.api.router`.

Builds a minimal FastAPI app on top of the in-memory SQLite session so
the tests don't need uvicorn or the main FinceptWeb app — just
TestClient. Auth is exercised in DEBUG=true mode (no key required) so
the tests focus on the routing / SQL / response-shape contract.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from finmind.api.router import router
from finmind.db.session import get_finmind_db
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.init_db import seed_dataset_sources


@pytest.fixture(autouse=True)
def _enable_dev_auth_bypass(monkeypatch):
    """Phase-3 stub auth opens up when DEBUG=true and no allowlist is
    set — matches local-dev affordance. Tests rely on this so we don't
    have to wire keys per test."""
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.delenv("FINMIND_API_KEYS_ALLOWLIST", raising=False)
    monkeypatch.delenv("FINMIND_ADMIN_API_KEY", raising=False)


@pytest.fixture
def app(finmind_session):
    """Build the test app with the in-memory session injected."""
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


@pytest.mark.asyncio
async def test_list_datasets_returns_full_catalog(client, finmind_session):
    await seed_dataset_sources()

    resp = await client.get("/api/finmind/datasets")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 80
    # Spot-check a known dataset's shape.
    by_code = {row["dataset_code"]: row for row in body}
    assert by_code["TaiwanStockPrice"]["local_table"] == "ohlcv_daily"
    assert by_code["TaiwanStockPrice"]["per_symbol"] is True


@pytest.mark.asyncio
async def test_public_catalog_returns_slim_shape(client, finmind_session):
    """`/catalog` drops operator fields and surfaces a single
    `available` flag. Used by the public marketing page."""
    await seed_dataset_sources()

    resp = await client.get("/api/finmind/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 80
    by_code = {row["dataset_code"]: row for row in body}
    sample = by_code["TaiwanStockPrice"]
    # Operator-only fields must be absent.
    assert "last_error" not in sample
    assert "last_ingest_at" not in sample
    assert "active_source" not in sample
    assert "primary_source" not in sample
    assert "local_table" not in sample
    # Marketing-facing fields are present.
    assert sample["category"]
    assert sample["description_zh"]
    assert "available" in sample
    # Seeded rows aren't enabled by default → not available yet.
    assert sample["available"] is False


@pytest.mark.asyncio
async def test_public_catalog_no_auth_required(
    finmind_session, monkeypatch, app
):
    """`/catalog` must work without `X-Finmind-API-Key` even when an
    allowlist is set — otherwise a prospect can't browse before
    signing up."""
    monkeypatch.setenv("FINMIND_API_KEYS_ALLOWLIST", "valid-key-1")
    monkeypatch.setenv("DEBUG", "false")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        resp = await ac.get("/api/finmind/catalog")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_data_404_unknown_dataset(client):
    resp = await client.get("/api/finmind/data/TotallyMadeUp")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_data_503_when_local_table_empty(client, finmind_session):
    """Realtime datasets intentionally have no destination — surface a
    503 with a clear "not yet built" message rather than a generic
    error."""
    await seed_dataset_sources()
    resp = await client.get("/api/finmind/data/taiwan_stock_tick_snapshot")
    assert resp.status_code == 503
    assert "not yet built" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_data_returns_rows_with_metadata(client, finmind_session):
    """End-to-end: insert sample rows into ohlcv_daily, query via API,
    verify FinMind-mirror response shape."""
    await seed_dataset_sources()

    await finmind_session.execute(
        text(
            "INSERT INTO ohlcv_daily "
            "(market, symbol, ts, open, high, low, close, volume, source) VALUES "
            "('TWSE', '2330', '2024-01-02', 600, 605, 598, 603, 12345, 'finmind'),"
            "('TWSE', '2330', '2024-01-03', 603, 608, 602, 606, 23456, 'finmind'),"
            "('TWSE', '2317', '2024-01-02', 100, 102, 99, 101, 5678, 'finmind')"
        )
    )
    await finmind_session.commit()

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330", "start_date": "2024-01-01", "end_date": "2024-01-31"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 200
    assert body["msg"] == "success"
    assert len(body["data"]) == 2
    # Newest first per the ORDER BY ts DESC.
    assert body["data"][0]["ts"] == "2024-01-03"
    assert body["data"][1]["ts"] == "2024-01-02"
    # Other symbol filtered out.
    for row in body["data"]:
        assert row["symbol"] == "2330"
    # Metadata.
    md = body["metadata"]
    assert md["dataset_code"] == "TaiwanStockPrice"
    assert md["local_table"] == "ohlcv_daily"
    assert md["active_source"] == "finmind"
    assert md["row_count"] == 2


@pytest.mark.asyncio
async def test_get_data_respects_limit(client, finmind_session):
    await seed_dataset_sources()
    rows = ", ".join(
        f"('TWSE', '2330', '2024-{m:02d}-01', 1, 1, 1, 1, 1, 'finmind')"
        for m in range(1, 13)
    )
    await finmind_session.execute(
        text(
            f"INSERT INTO ohlcv_daily "
            f"(market, symbol, ts, open, high, low, close, volume, source) "
            f"VALUES {rows}"
        )
    )
    await finmind_session.commit()

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330", "limit": 3},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 3


@pytest.mark.asyncio
async def test_admin_endpoints_503_without_key_env(client, finmind_session):
    """Admin requires an explicit env-var setup — until set, every admin
    call returns 503 to make the feature gate obvious."""
    resp = await client.get("/api/finmind/admin/datasets")
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_update_toggles_enabled(
    client, finmind_session, monkeypatch
):
    monkeypatch.setenv("FINMIND_ADMIN_API_KEY", "test-admin-secret")

    await seed_dataset_sources()

    resp = await client.patch(
        "/api/finmind/admin/datasets/TaiwanStockPrice",
        json={"enabled": True},
        headers={"X-Finmind-Admin-Key": "test-admin-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True

    # Verify side effect.
    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    await finmind_session.refresh(row)
    assert row.enabled is True


@pytest.mark.asyncio
async def test_admin_update_validates_active_source(
    client, finmind_session, monkeypatch
):
    monkeypatch.setenv("FINMIND_ADMIN_API_KEY", "test-admin-secret")

    await seed_dataset_sources()

    resp = await client.patch(
        "/api/finmind/admin/datasets/TaiwanStockPrice",
        json={"active_source": "definitely_not_a_source"},
        headers={"X-Finmind-Admin-Key": "test-admin-secret"},
    )
    assert resp.status_code == 400
    assert "active_source must be" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_update_rejects_unimplemented_source(
    client, finmind_session, monkeypatch
):
    """Flipping `active_source` to a stubbed connector (tpex / taifex /
    tdcc currently) must 400 — otherwise the next cron tick blows up
    with NotImplementedError and the operator only finds out from the
    failed-ingest count hours later."""
    monkeypatch.setenv("FINMIND_ADMIN_API_KEY", "test-admin-secret")

    await seed_dataset_sources()

    # tpex is registered as a stub — _NotWiredYetClient.
    resp = await client.patch(
        "/api/finmind/admin/datasets/TaiwanStockPrice",
        json={"active_source": "tpex"},
        headers={"X-Finmind-Admin-Key": "test-admin-secret"},
    )
    assert resp.status_code == 400
    assert "no connector wired up yet" in resp.json()["detail"]
    assert "tpex" in resp.json()["detail"]

    # twse IS wired — same endpoint, same auth, just a real source.
    resp = await client.patch(
        "/api/finmind/admin/datasets/TaiwanStockPrice",
        json={"active_source": "twse"},
        headers={"X-Finmind-Admin-Key": "test-admin-secret"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_phase_a_to_b_transition(
    client, finmind_session, monkeypatch
):
    """Phase A → B switch is the headline operator action — verify it's
    a single PATCH and the read API immediately reflects the new
    `active_source` in response metadata."""
    monkeypatch.setenv("FINMIND_ADMIN_API_KEY", "test-admin-secret")

    await seed_dataset_sources()

    resp = await client.patch(
        "/api/finmind/admin/datasets/TaiwanStockPrice",
        json={"active_source": "twse"},
        headers={"X-Finmind-Admin-Key": "test-admin-secret"},
    )
    assert resp.status_code == 200

    await finmind_session.execute(
        text(
            "INSERT INTO ohlcv_daily "
            "(market, symbol, ts, open, high, low, close, volume, source) VALUES "
            "('TWSE', '2330', '2024-01-02', 600, 605, 598, 603, 12345, 'twse')"
        )
    )
    await finmind_session.commit()

    resp = await client.get(
        "/api/finmind/data/TaiwanStockPrice",
        params={"data_id": "2330"},
    )
    assert resp.json()["metadata"]["active_source"] == "twse"


@pytest.mark.asyncio
async def test_auth_required_when_allowlist_set(
    finmind_session, monkeypatch, app
):
    """When the allowlist env var is set, missing key → 401."""
    monkeypatch.setenv("FINMIND_API_KEYS_ALLOWLIST", "valid-key-1,valid-key-2")
    monkeypatch.setenv("DEBUG", "false")
    # Recreate client with new env so the auth dep re-reads it.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        resp = await ac.get("/api/finmind/datasets")
        assert resp.status_code == 401

        resp = await ac.get(
            "/api/finmind/datasets",
            headers={"X-Finmind-API-Key": "valid-key-1"},
        )
        # 200 — auth passes. Catalog will be empty since this test
        # doesn't seed.
        assert resp.status_code == 200
