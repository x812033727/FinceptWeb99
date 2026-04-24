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


# ── change password ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_password_success(client: AsyncClient):
    await client.post("/api/auth/register", json={"email": "chpw@example.com", "password": "OldPass99!"})
    login = await client.post("/api/auth/login", json={"email": "chpw@example.com", "password": "OldPass99!"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch("/api/auth/me", json={"current_password": "OldPass99!", "new_password": "NewPass99!"}, headers=headers)
    assert r.status_code == 204

    # Old password no longer works
    bad = await client.post("/api/auth/login", json={"email": "chpw@example.com", "password": "OldPass99!"})
    assert bad.status_code == 401

    # New password works
    good = await client.post("/api/auth/login", json={"email": "chpw@example.com", "password": "NewPass99!"})
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current(client: AsyncClient):
    await client.post("/api/auth/register", json={"email": "chpw_bad@example.com", "password": "RealPass99!"})
    login = await client.post("/api/auth/login", json={"email": "chpw_bad@example.com", "password": "RealPass99!"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.patch("/api/auth/me", json={"current_password": "WrongPass!", "new_password": "NewPass99!"}, headers=headers)
    assert r.status_code in (400, 401, 403)


@pytest.mark.asyncio
async def test_change_password_requires_auth(client: AsyncClient):
    r = await client.patch("/api/auth/me", json={"current_password": "x", "new_password": "y"})
    assert r.status_code == 401


# ── API keys ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_key_create_and_list(client: AsyncClient):
    await client.post("/api/auth/register", json={"email": "apikey@example.com", "password": "Pass99!!"})
    login = await client.post("/api/auth/login", json={"email": "apikey@example.com", "password": "Pass99!!"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Create key
    cr = await client.post("/api/auth/api-keys", json={"name": "ci-key"}, headers=headers)
    assert cr.status_code == 201
    body = cr.json()
    assert "key" in body
    assert body["name"] == "ci-key"
    raw_key = body["key"]
    assert len(raw_key) > 20

    # List should contain it
    lr = await client.get("/api/auth/api-keys", headers=headers)
    assert lr.status_code == 200
    names = [k["name"] for k in lr.json()]
    assert "ci-key" in names


@pytest.mark.asyncio
async def test_api_key_delete(client: AsyncClient):
    await client.post("/api/auth/register", json={"email": "apikey_del@example.com", "password": "Pass99!!"})
    login = await client.post("/api/auth/login", json={"email": "apikey_del@example.com", "password": "Pass99!!"})
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    cr = await client.post("/api/auth/api-keys", json={"name": "temp-key"}, headers=headers)
    key_id = cr.json()["id"]

    dr = await client.delete(f"/api/auth/api-keys/{key_id}", headers=headers)
    assert dr.status_code == 204

    lr = await client.get("/api/auth/api-keys", headers=headers)
    assert all(k["id"] != key_id for k in lr.json())


@pytest.mark.asyncio
async def test_api_key_requires_auth(client: AsyncClient):
    r = await client.get("/api/auth/api-keys")
    assert r.status_code == 401
