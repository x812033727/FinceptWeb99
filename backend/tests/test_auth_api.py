"""
Integration tests for auth endpoints using in-memory SQLite + mocked Redis.
These run without Postgres or a real Redis instance.
"""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_register_and_login(client: AsyncClient):
    # Register
    resp = await client.post("/api/auth/register", json={
        "email": "test@example.com",
        "password": "TestPass123!",
    })
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert "access_token" in body

    # Login
    resp2 = await client.post("/api/auth/login", json={
        "email": "test@example.com",
        "password": "TestPass123!",
    })
    assert resp2.status_code == 200
    assert "access_token" in resp2.json()


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient):
    await client.post("/api/auth/register", json={
        "email": "wrongpw@example.com",
        "password": "CorrectHorse99",
    })
    resp = await client.post("/api/auth/login", json={
        "email": "wrongpw@example.com",
        "password": "BadPassword",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client: AsyncClient):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_token(client: AsyncClient):
    await client.post("/api/auth/register", json={
        "email": "me@example.com",
        "password": "ValidPass99!",
    })
    login = await client.post("/api/auth/login", json={
        "email": "me@example.com",
        "password": "ValidPass99!",
    })
    token = login.json()["access_token"]

    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"


@pytest.mark.asyncio
async def test_duplicate_register(client: AsyncClient):
    payload = {"email": "dup@example.com", "password": "Pass1234!"}
    await client.post("/api/auth/register", json=payload)
    resp = await client.post("/api/auth/register", json=payload)
    assert resp.status_code in (400, 409)


@pytest.mark.asyncio
async def test_portfolio_requires_auth(client: AsyncClient):
    resp = await client.get("/api/portfolio")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_watchlist_requires_auth(client: AsyncClient):
    resp = await client.get("/api/watchlist")
    assert resp.status_code == 401
