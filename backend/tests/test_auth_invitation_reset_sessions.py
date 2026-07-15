import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.user import User, UserRole


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register_login(client: AsyncClient, email: str, password: str = "Test1234!") -> str:
    await client.post("/api/auth/register", json={"email": email, "password": password})
    response = await client.post("/api/auth/login", json={"email": email, "password": password})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_public_registration_is_closed_when_disabled(client: AsyncClient):
    previous = settings.PUBLIC_REGISTRATION_ENABLED
    settings.PUBLIC_REGISTRATION_ENABLED = False
    try:
        response = await client.post("/api/auth/register", json={
            "email": "closed@example.com", "password": "Test1234!",
        })
    finally:
        settings.PUBLIC_REGISTRATION_ENABLED = previous
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_invitation_is_email_bound_and_single_use(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "invite-admin@example.com"
    await _register_login(client, email)
    admin = await db_session.scalar(select(User).where(User.email == email))
    admin.role = UserRole.admin
    await db_session.commit()
    admin_token = (await client.post("/api/auth/login", json={
        "email": email, "password": "Test1234!",
    })).json()["access_token"]

    created = await client.post("/api/admin/invitations", headers=_auth(admin_token), json={
        "email": "analyst@example.com", "role": "analyst", "expires_hours": 2,
    })
    assert created.status_code == 201, created.text
    raw_token = created.json()["token"]

    mismatch = await client.post("/api/auth/accept-invite", json={
        "token": raw_token, "email": "other@example.com", "password": "StrongPass9!",
    })
    assert mismatch.status_code == 400

    accepted = await client.post("/api/auth/accept-invite", json={
        "token": raw_token, "email": "analyst@example.com", "password": "StrongPass9!",
    })
    assert accepted.status_code == 201, accepted.text
    user = await db_session.scalar(select(User).where(User.email == "analyst@example.com"))
    assert user.role == UserRole.analyst

    replay = await client.post("/api/auth/accept-invite", json={
        "token": raw_token, "email": "analyst@example.com", "password": "StrongPass9!",
    })
    assert replay.status_code == 400


@pytest.mark.asyncio
async def test_password_reset_is_single_use_and_changes_password(client: AsyncClient):
    await _register_login(client, "reset@example.com", "OldPass99!")
    sender = AsyncMock()
    with patch("api.auth.router.send_password_reset_email", sender):
        forgot = await client.post("/api/auth/password/forgot", json={"email": "reset@example.com"})
    assert forgot.status_code == 202
    sender.assert_awaited_once()
    raw_token = sender.await_args.args[1]

    reset = await client.post("/api/auth/password/reset", json={
        "token": raw_token, "new_password": "NewPass99!",
    })
    assert reset.status_code == 204, reset.text
    assert (await client.post("/api/auth/login", json={
        "email": "reset@example.com", "password": "OldPass99!",
    })).status_code == 401
    assert (await client.post("/api/auth/login", json={
        "email": "reset@example.com", "password": "NewPass99!",
    })).status_code == 200
    assert (await client.post("/api/auth/password/reset", json={
        "token": raw_token, "new_password": "Another99!",
    })).status_code == 400


@pytest.mark.asyncio
async def test_forgot_password_does_not_disclose_unknown_email(client: AsyncClient):
    sender = AsyncMock()
    with patch("api.auth.router.send_password_reset_email", sender):
        response = await client.post("/api/auth/password/forgot", json={
            "email": "unknown@example.com",
        })
    assert response.status_code == 202
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_account_scoped_auth_responses_are_never_cached(client: AsyncClient):
    token = await _register_login(client, "no-cache@example.com")
    for path in ("/api/auth/me", "/api/auth/consents"):
        response = await client.get(path, headers=_auth(token))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["pragma"] == "no-cache"
        assert "Authorization" in response.headers["vary"]
        assert "Cookie" in response.headers["vary"]


@pytest.mark.asyncio
async def test_sessions_are_owner_scoped_and_revocable(client: AsyncClient, mock_redis):
    token = await _register_login(client, "sessions@example.com")
    mock_redis.smembers.return_value = {b"session-a"}
    mock_redis.get.return_value = json.dumps({
        "created_at": "2026-07-15T00:00:00+00:00",
        "ip_address": "127.0.0.1",
        "user_agent": "pytest",
    })

    redis_getter = AsyncMock(return_value=mock_redis)
    with patch("api.auth.router.get_redis", redis_getter):
        listed = await client.get("/api/auth/sessions", headers=_auth(token))
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == "session-a"

    mock_redis.sismember.return_value = False
    with patch("api.auth.router.get_redis", redis_getter):
        not_owned = await client.delete("/api/auth/sessions/not-owned", headers=_auth(token))
    assert not_owned.status_code == 404
    mock_redis.sismember.return_value = True
    with patch("api.auth.router.get_redis", redis_getter):
        deleted = await client.delete("/api/auth/sessions/session-a", headers=_auth(token))
    assert deleted.status_code == 204
    mock_redis.srem.assert_awaited()

    with patch("api.auth.router.get_redis", redis_getter):
        deleted_all = await client.delete("/api/auth/sessions", headers=_auth(token))
    assert deleted_all.status_code == 204
