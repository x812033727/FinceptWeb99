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


# ── /api/alerts/history (PR-D5) ──────────────────────────────────────

async def _seed_events(db_session, email: str, symbols: list[str]):
    """Insert alert_events rows (one per symbol, 1 min apart) for the
    registered user with this email."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from models.alert import AlertEvent
    from models.user import User

    user = await db_session.scalar(select(User).where(User.email == email))
    base = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    for i, sym in enumerate(symbols):
        db_session.add(AlertEvent(
            user_id=user.id, symbol=sym, market="US", kind="price",
            message=f"{sym} fired", fired_at=base + timedelta(minutes=i),
        ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_history_empty(client: AsyncClient):
    h = await _auth_headers(client, "hist_empty@example.com")
    r = await client.get("/api/alerts/history", headers=h)
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_history_requires_auth(client: AsyncClient):
    r = await client.get("/api/alerts/history")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_history_newest_first_and_paginated(client: AsyncClient, db_session):
    h = await _auth_headers(client, "hist_page@example.com")
    await _seed_events(db_session, "hist_page@example.com", ["A1", "A2", "A3"])

    r = await client.get("/api/alerts/history?limit=2", headers=h)
    assert r.status_code == 200
    page1 = r.json()
    assert [e["symbol"] for e in page1] == ["A3", "A2"]

    r = await client.get(
        f"/api/alerts/history?limit=2&before={page1[-1]['fired_at']}",
        headers=h,
    )
    page2 = r.json()
    assert [e["symbol"] for e in page2] == ["A1"]


@pytest.mark.asyncio
async def test_history_no_cross_user_leak(client: AsyncClient, db_session):
    h1 = await _auth_headers(client, "hist_u1@example.com")
    h2 = await _auth_headers(client, "hist_u2@example.com")
    await _seed_events(db_session, "hist_u1@example.com", ["OWN1"])

    r1 = (await client.get("/api/alerts/history", headers=h1)).json()
    r2 = (await client.get("/api/alerts/history", headers=h2)).json()
    assert [e["symbol"] for e in r1] == ["OWN1"]
    assert r2 == []


@pytest.mark.asyncio
async def test_history_limit_validation(client: AsyncClient):
    h = await _auth_headers(client, "hist_lim@example.com")
    r = await client.get("/api/alerts/history?limit=0", headers=h)
    assert r.status_code == 422
    r = await client.get("/api/alerts/history?limit=500", headers=h)
    assert r.status_code == 422
