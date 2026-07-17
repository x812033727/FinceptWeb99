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
async def test_default_order_is_first_column_desc(client, db_session):
    email = "db_admin_order@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/db/tables/public/users/rows", headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.json()
    first_col = body["columns"][0]["name"]
    values = [str(row[0]) for row in body["rows"] if row[0] is not None]
    assert values == sorted(values, reverse=True), (
        f"rows not ordered by {first_col} desc"
    )


@pytest.mark.asyncio
async def test_export_csv_masks_and_filters(client, db_session):
    email = "db_admin_export@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)

    r = await client.get(
        "/api/admin/db/tables/public/users/export.csv"
        f"?filter_col=email&filter_op=eq&filter_val={email}",
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="public.users.csv"' in r.headers["content-disposition"]

    import csv as _csv
    import io as _io

    rows = list(_csv.reader(_io.StringIO(r.text)))
    header, data = rows[0], rows[1:]
    assert len(data) == 1
    assert data[0][header.index("email")] == email
    pw_cols = [c for c in header if "password" in c.lower()]
    assert pw_cols
    pw_val = data[0][header.index(pw_cols[0])]
    assert pw_val in ("***", "")


@pytest.mark.asyncio
async def test_export_csv_rejects_garbage(client, db_session):
    email = "db_admin_export_bad@test.com"
    await _register_login(client, email)
    token = await _promote_to_admin(db_session, email, client)
    h = _auth(token)

    r = await client.get(
        "/api/admin/db/tables/pg_catalog/pg_class/export.csv", headers=h,
    )
    assert r.status_code == 404
    r = await client.get(
        "/api/admin/db/tables/public/users/export.csv?order_by=no_such_col",
        headers=h,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_query_parts_default_order_direct(db_session, client):
    """Direct-call coverage of the shared validation helper: the
    HTTP-layer tests above exercise it too, but the coverage tracer
    doesn't always see lines run inside the ASGI dispatch."""
    from api.admin.db_browser import _validated_query_parts

    await client.post(
        "/api/auth/register",
        json={"email": "seed_direct@test.com", "password": "Test1234!"},
    )
    columns, qualified, filter_sql, order_sql, params = await _validated_query_parts(
        db_session, "public", "users", None, "desc", None, None, None,
    )
    first_col = columns[0][0]
    assert order_sql == f' ORDER BY "{first_col}" DESC'
    assert filter_sql == "" and params == {}
    assert qualified == '"users"'  # SQLite: unqualified


@pytest.mark.asyncio
async def test_rows_and_export_direct(db_session, client, monkeypatch):
    from api.admin.db_browser import export_csv, table_rows

    email = "seed_export_direct@test.com"
    await client.post(
        "/api/auth/register", json={"email": email, "password": "Test1234!"},
    )
    admin = {"id": "test-admin"}

    page = await table_rows(
        "public", "users", admin, db_session,
        page=1, page_size=99999, order_by=None, order_dir="desc",
        filter_col=None, filter_op=None, filter_val=None,
    )
    assert page.page_size == 500  # clamp
    assert page.total >= 1

    # Tiny flush threshold → exercises the mid-stream buffer flush.
    # (The generator reads the module global at iteration time, so the
    # monkeypatch must stay active until the chunks are consumed.)
    import api.admin.db_browser as mod
    monkeypatch.setattr(mod, "_CSV_FLUSH_BYTES", 1)
    resp = await export_csv(
        "public", "users", admin, db_session,
        order_by="email", order_dir="asc",
        filter_col="email", filter_op="eq", filter_val=email,
    )
    assert resp.media_type.startswith("text/csv")
    chunks = [chunk async for chunk in resp.body_iterator]
    text = "".join(c if isinstance(c, str) else c.decode() for c in chunks)
    import csv as _csv
    import io as _io

    rows = list(_csv.reader(_io.StringIO(text)))
    header, data = rows[0], rows[1:]
    assert len(data) == 1
    assert data[0][header.index("email")] == email


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
