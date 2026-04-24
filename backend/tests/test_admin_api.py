"""
Integration tests for admin endpoints.
Uses in-memory SQLite + mocked Redis (from conftest).
Admin actions require a user with role=admin.
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole


# ── helpers ────────────────────────────────────────────────────────

async def _register_login(client: AsyncClient, email: str, password: str = "Test1234!") -> str:
    await client.post("/api/auth/register", json={"email": email, "password": password})
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _promote_to_admin(
    db: AsyncSession,
    email: str,
    client: AsyncClient | None = None,
    password: str = "Test1234!",
) -> str | None:
    """
    Directly set a user's role to admin in the test DB.
    If `client` is provided, re-login and return a fresh token whose JWT
    claim reflects the new role (JWTs are stateless — the old token still
    carries role=viewer).
    """
    from sqlalchemy import select
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one()
    user.role = UserRole.admin
    await db.commit()

    if client is not None:
        r = await client.post("/api/auth/login", json={"email": email, "password": password})
        return r.json()["access_token"]
    return None


# ── access control ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_requires_auth(client: AsyncClient):
    r = await client.get("/api/admin/stats")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_requires_admin_role(client: AsyncClient):
    token = await _register_login(client, "viewer_admin@test.com")
    r = await client.get("/api/admin/stats", headers=_auth(token))
    assert r.status_code == 403


# ── stats ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_stats_shape(client: AsyncClient, db_session: AsyncSession):
    email = "admin_stats@test.com"
    token = await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    r = await client.get("/api/admin/stats", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert "total_users" in body
    assert "active_users" in body
    assert "users_by_role" in body
    assert "total_alerts" in body
    assert "total_watchlists" in body
    assert body["total_users"] >= 1
    assert body["active_users"] >= 1


@pytest.mark.asyncio
async def test_admin_stats_counts_multiple_users(client: AsyncClient, db_session: AsyncSession):
    admin_email = "admin_count@test.com"
    admin_tok = await _register_login(client, admin_email)
    admin_tok = await _promote_to_admin(db_session, admin_email, client=client)

    # Register two more users
    await _register_login(client, "extra1_count@test.com")
    await _register_login(client, "extra2_count@test.com")

    r = await client.get("/api/admin/stats", headers=_auth(admin_tok))
    assert r.status_code == 200
    assert r.json()["total_users"] >= 3


# ── user listing ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_list_users(client: AsyncClient, db_session: AsyncSession):
    email = "admin_list@test.com"
    token = await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    r = await client.get("/api/admin/users", headers=_auth(token))
    assert r.status_code == 200
    users = r.json()
    assert isinstance(users, list)
    assert len(users) >= 1
    # Check fields present
    u = users[0]
    assert "id" in u
    assert "email" in u
    assert "role" in u
    assert "is_active" in u
    assert "created_at" in u


@pytest.mark.asyncio
async def test_admin_list_users_pagination(client: AsyncClient, db_session: AsyncSession):
    email = "admin_page@test.com"
    token = await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    # Create extra users
    for i in range(3):
        await _register_login(client, f"page_user_{i}@test.com")

    r_limited = await client.get("/api/admin/users?limit=1", headers=_auth(token))
    assert r_limited.status_code == 200
    assert len(r_limited.json()) == 1


# ── role update ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_update_user_role(client: AsyncClient, db_session: AsyncSession):
    admin_email = "admin_role@test.com"
    admin_tok = await _register_login(client, admin_email)
    admin_tok = await _promote_to_admin(db_session, admin_email, client=client)

    target_email = "target_role@test.com"
    await _register_login(client, target_email)

    # Get target user id
    users_r = await client.get("/api/admin/users", headers=_auth(admin_tok))
    target = next(u for u in users_r.json() if u["email"] == target_email)
    uid = target["id"]

    # Promote to analyst
    r = await client.patch(f"/api/admin/users/{uid}/role", json={"role": "analyst"}, headers=_auth(admin_tok))
    assert r.status_code == 204

    # Verify in user list
    users_r2 = await client.get("/api/admin/users", headers=_auth(admin_tok))
    updated = next(u for u in users_r2.json() if u["id"] == uid)
    assert updated["role"] == "analyst"


@pytest.mark.asyncio
async def test_admin_update_role_invalid(client: AsyncClient, db_session: AsyncSession):
    email = "admin_badrole@test.com"
    token = await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    users_r = await client.get("/api/admin/users", headers=_auth(token))
    uid = users_r.json()[0]["id"]

    r = await client.patch(f"/api/admin/users/{uid}/role", json={"role": "superuser"}, headers=_auth(token))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_admin_update_role_nonexistent_user(client: AsyncClient, db_session: AsyncSession):
    email = "admin_nouser@test.com"
    token = await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    r = await client.patch(
        "/api/admin/users/00000000-0000-0000-0000-000000000000/role",
        json={"role": "analyst"},
        headers=_auth(token),
    )
    assert r.status_code == 404


# ── active toggle ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_disable_and_enable_user(client: AsyncClient, db_session: AsyncSession):
    admin_email = "admin_toggle@test.com"
    admin_tok = await _register_login(client, admin_email)
    admin_tok = await _promote_to_admin(db_session, admin_email, client=client)

    target_email = "target_toggle@test.com"
    await _register_login(client, target_email)

    users_r = await client.get("/api/admin/users", headers=_auth(admin_tok))
    target = next(u for u in users_r.json() if u["email"] == target_email)
    uid = target["id"]

    # Disable
    r = await client.patch(f"/api/admin/users/{uid}/active", json={"is_active": False}, headers=_auth(admin_tok))
    assert r.status_code == 204

    users_r2 = await client.get("/api/admin/users", headers=_auth(admin_tok))
    disabled = next(u for u in users_r2.json() if u["id"] == uid)
    assert disabled["is_active"] is False

    # Re-enable
    r2 = await client.patch(f"/api/admin/users/{uid}/active", json={"is_active": True}, headers=_auth(admin_tok))
    assert r2.status_code == 204

    users_r3 = await client.get("/api/admin/users", headers=_auth(admin_tok))
    enabled = next(u for u in users_r3.json() if u["id"] == uid)
    assert enabled["is_active"] is True


@pytest.mark.asyncio
async def test_admin_active_nonexistent_user(client: AsyncClient, db_session: AsyncSession):
    email = "admin_noact@test.com"
    token = await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client=client)

    r = await client.patch(
        "/api/admin/users/00000000-0000-0000-0000-000000000000/active",
        json={"is_active": False},
        headers=_auth(token),
    )
    assert r.status_code == 404
