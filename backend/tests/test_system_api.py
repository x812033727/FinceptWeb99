"""HTTP-level tests for /api/system/version and /api/admin/update."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole


async def _register_login(client: AsyncClient, email: str, password: str = "Test1234!") -> str:
    await client.post("/api/auth/register", json={"email": email, "password": password})
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _promote_to_admin(db: AsyncSession, email: str, client: AsyncClient, password: str = "Test1234!") -> str:
    from sqlalchemy import select
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    user.role = UserRole.admin
    await db.commit()
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.json()["access_token"]


# ── GET /api/system/version ───────────────────────────────────────

@pytest.mark.asyncio
async def test_version_requires_auth(client: AsyncClient):
    r = await client.get("/api/system/version")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_version_returns_status_to_logged_in_user(client: AsyncClient):
    token = await _register_login(client, "version_user@test.com")
    fake = {
        "current": "0.1.0",
        "latest": "0.1.0",
        "update_available": False,
        "html_url": "",
        "published_at": "",
    }
    with patch("api.system.router.get_version_status", AsyncMock(return_value=fake)):
        r = await client.get("/api/system/version", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == "0.1.0"
    assert body["update_available"] is False


@pytest.mark.asyncio
async def test_version_reports_update_available(client: AsyncClient):
    token = await _register_login(client, "version_avail@test.com")
    fake = {
        "current": "0.1.0",
        "latest": "0.2.0",
        "update_available": True,
        "html_url": "https://example.com/r",
        "published_at": "2026-04-25T00:00:00Z",
    }
    with patch("api.system.router.get_version_status", AsyncMock(return_value=fake)):
        r = await client.get("/api/system/version", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["update_available"] is True
    assert r.json()["latest"] == "0.2.0"


# ── POST /api/admin/update ────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_requires_admin_role(client: AsyncClient):
    token = await _register_login(client, "viewer_update@test.com")
    r = await client.post("/api/admin/update", headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_update_requires_auth(client: AsyncClient):
    r = await client.post("/api/admin/update")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_update_admin_not_configured(client: AsyncClient, db_session: AsyncSession):
    email = "admin_update_noop@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    fake = {"status": "not_configured", "message": "UPDATE_COMMAND is empty"}
    with patch("api.admin.router.trigger_update", AsyncMock(return_value=fake)):
        r = await client.post("/api/admin/update", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "not_configured"


@pytest.mark.asyncio
async def test_update_admin_started(client: AsyncClient, db_session: AsyncSession):
    email = "admin_update_go@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    fake = {"status": "started", "message": "update command dispatched"}
    with patch("api.admin.router.trigger_update", AsyncMock(return_value=fake)):
        r = await client.post("/api/admin/update", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "started"


# ── POST /api/admin/version/check ─────────────────────────────────

@pytest.mark.asyncio
async def test_version_check_requires_auth(client: AsyncClient):
    r = await client.post("/api/admin/version/check")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_version_check_requires_admin_role(client: AsyncClient):
    token = await _register_login(client, "viewer_check@test.com")
    r = await client.post("/api/admin/version/check", headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_version_check_returns_fresh_status(client: AsyncClient, db_session: AsyncSession):
    email = "admin_check@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    fresh = {
        "current": "0.1.0",
        "latest": "0.2.0",
        "update_available": True,
        "html_url": "https://example.com/r",
        "published_at": "2026-04-25T00:00:00Z",
    }
    with patch("api.admin.router.force_refresh_status", AsyncMock(return_value=fresh)) as mock:
        r = await client.post("/api/admin/version/check", headers=_auth(token))
    mock.assert_awaited_once()
    assert r.status_code == 200
    body = r.json()
    assert body["latest"] == "0.2.0"
    assert body["update_available"] is True
