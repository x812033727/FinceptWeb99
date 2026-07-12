"""Admin read-only DB browser — auth gate, catalog validation,
pagination, masking, and injection resistance.

Runs on the shared in-memory SQLite app fixture, exercising the SQLite
branch of the dialect switch; the Postgres branch differs only in
catalog queries (pg_class / information_schema) and the statement
timeout, both plain SQL.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole


async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post(
        "/api/auth/register", json={"email": email, "password": "Test1234!"},
    )
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "Test1234!"},
    )
    return r.json()["access_token"]


async def _promote_to_admin(db: AsyncSession, email: str, client: AsyncClient) -> str:
    from sqlalchemy import select

    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    user.role = UserRole.admin
    await db.commit()
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "Test1234!"},
    )
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_non_admin_gets_403(client):
    token = await _register_login(client, "db_viewer@test.com")
    r = await client.get("/api/admin/db/tables", headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_tables_list_contains_users(client, db_session):
    email = "db_admin_list@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get("/api/admin/db/tables", headers=_auth(token))
    assert r.status_code == 200
    names = {t["table"] for t in r.json()}
    assert "users" in names


@pytest.mark.asyncio
async def test_rows_paginates_and_masks_credentials(client, db_session):
    email = "db_admin_rows@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/db/tables/public/users/rows?page=1&page_size=10",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    col_names = [c["name"] for c in body["columns"]]
    assert "email" in col_names

    # Credential column is flagged AND its values are masked.
    pw_cols = [c for c in body["columns"] if "password" in c["name"].lower()]
    assert pw_cols and all(c["masked"] for c in pw_cols)
    pw_idx = col_names.index(pw_cols[0]["name"])
    assert all(row[pw_idx] in ("***", None) for row in body["rows"])
    # Non-masked values come through.
    email_idx = col_names.index("email")
    assert any(row[email_idx] == email for row in body["rows"])


@pytest.mark.asyncio
async def test_filter_and_order(client, db_session):
    email = "db_admin_filter@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/db/tables/public/users/rows"
        f"?filter_col=email&filter_op=eq&filter_val={email}"
        "&order_by=email&order_dir=asc",
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    email_idx = [c["name"] for c in body["columns"]].index("email")
    assert body["rows"][0][email_idx] == email


@pytest.mark.asyncio
async def test_rejects_schema_table_column_and_op_garbage(client, db_session):
    email = "db_admin_garbage@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)
    h = _auth(token)

    # Schema outside the allowlist.
    r = await client.get("/api/admin/db/tables/pg_catalog/pg_class/rows", headers=h)
    assert r.status_code == 404

    # Injection-shaped table name → catalog miss → 404, no query built.
    r = await client.get(
        "/api/admin/db/tables/public/users%3B%20DROP%20TABLE%20users/rows", headers=h,
    )
    assert r.status_code == 404

    # Unknown order_by column.
    r = await client.get(
        "/api/admin/db/tables/public/users/rows?order_by=no_such_col", headers=h,
    )
    assert r.status_code == 400

    # Injection-shaped filter column.
    r = await client.get(
        "/api/admin/db/tables/public/users/rows"
        "?filter_col=email%3B--&filter_op=eq&filter_val=x",
        headers=h,
    )
    assert r.status_code == 400

    # Unsupported operator.
    r = await client.get(
        "/api/admin/db/tables/public/users/rows"
        "?filter_col=email&filter_op=regex&filter_val=x",
        headers=h,
    )
    assert r.status_code == 400

    # Incomplete filter triple.
    r = await client.get(
        "/api/admin/db/tables/public/users/rows?filter_col=email", headers=h,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_page_size_clamped_to_500(client, db_session):
    email = "db_admin_clamp@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/db/tables/public/users/rows?page_size=99999",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.json()["page_size"] == 500
