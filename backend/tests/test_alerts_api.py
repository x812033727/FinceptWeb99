"""Integration tests for price alert CRUD HTTP endpoints.

check_and_fire logic is covered comprehensively (19 tests, boundary
cases, multi-user fan-out, double-fire guard, market mismatch) in
test_alert_service.py — as pure unit tests that don't require the
FastAPI test client. Only HTTP-surface behavior lives here.
"""
import pytest
from httpx import AsyncClient


async def _auth_headers(client: AsyncClient, email: str = "alerts@example.com") -> dict[str, str]:
    await client.post("/api/auth/register", json={
        "email": email,
        "password": "ValidPass99!",
    })
    r = await client.post("/api/auth/login", json={
        "email": email,
        "password": "ValidPass99!",
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_list_alerts_empty(client: AsyncClient):
    h = await _auth_headers(client, "list_empty@example.com")
    r = await client.get("/api/alerts", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_create_alert(client: AsyncClient):
    h = await _auth_headers(client, "create_alert@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL",
        "market": "US",
        "condition": "above",
        "target_price": 200.0,
    }, headers=h)
    assert r.status_code == 201
    data = r.json()
    assert data["symbol"] == "AAPL"
    assert data["condition"] == "above"
    assert data["target_price"] == 200.0
    assert data["triggered"] is False


@pytest.mark.asyncio
async def test_create_alert_invalid_market(client: AsyncClient):
    h = await _auth_headers(client, "bad_market@example.com")
    r = await client.post("/api/alerts", json={
        "symbol": "AAPL",
        "market": "JP",
        "condition": "above",
        "target_price": 100.0,
    }, headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_delete_alert(client: AsyncClient):
    h = await _auth_headers(client, "del_alert@example.com")
    create_r = await client.post("/api/alerts", json={
        "symbol": "TSLA",
        "market": "US",
        "condition": "below",
        "target_price": 150.0,
    }, headers=h)
    alert_id = create_r.json()["id"]

    r = await client.delete(f"/api/alerts/{alert_id}", headers=h)
    assert r.status_code == 204

    alerts = (await client.get("/api/alerts", headers=h)).json()
    assert all(a["id"] != alert_id for a in alerts)


@pytest.mark.asyncio
async def test_delete_nonexistent_alert(client: AsyncClient):
    h = await _auth_headers(client, "del_none@example.com")
    import uuid
    r = await client.delete(f"/api/alerts/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_alert_requires_auth(client: AsyncClient):
    r = await client.get("/api/alerts")
    assert r.status_code == 401
