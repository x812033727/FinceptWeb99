"""Admin read-only database browser.

Backs the AdminPage「DB」tab: list every table in the allowlisted
schemas with size/row estimates, page through any table's rows with
optional ordering and a single-column filter (first catalog column
DESC when no order is given, so pagination is deterministic), and
export the current view as CSV (capped, same masking).

Security model (this endpoint hands an admin arbitrary-table reads, so
every layer is belt-and-braces):

  - `require_admin` JWT gate — same as every other admin route.
  - Schema allowlist is HARDCODED to {public, finmind}. No other
    namespace is reachable regardless of input.
  - Table and column names are never interpolated from user input:
    the requested names are checked against the live catalog
    (pg_class/information_schema on Postgres, sqlite_master/PRAGMA on
    SQLite tests) and only the CATALOG's own spelling — safely quoted —
    is spliced into SQL. Unknown names → 404/400 before any query.
  - Filter values travel as bound parameters; the operator comes from
    a fixed four-entry map (eq/ilike/gte/lte).
  - Page size is clamped to ≤500; on Postgres a `SET LOCAL
    statement_timeout` bounds each query at 5s. (We deliberately do
    NOT issue `SET TRANSACTION READ ONLY`: the request's session has
    already executed the auth lookup in this transaction, and PG only
    accepts that flag before the first statement. The read-only
    guarantee here comes from this module only ever building SELECTs.)
  - Sensitive columns (password/secret/token/encrypted/hashed/
    credential) are masked server-side to "***" — an admin browsing
    users must not see credential material even in ciphertext.
  - Every rows query is audit-logged with the admin's user id.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db

log = logging.getLogger("api.admin.db_browser")

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]

SCHEMA_ALLOWLIST = ("public", "finmind")

# Any column whose lowercase name contains one of these fragments has
# its values masked in responses.
_MASK_FRAGMENTS = ("password", "secret", "token", "encrypted", "hashed", "credential")

_FILTER_OPS = {"eq": "=", "ilike": "ILIKE", "gte": ">=", "lte": "<="}

_MAX_PAGE_SIZE = 500

# CSV export is bounded so an admin can't pull a 100M-row finmind table
# through the app in one request.
_EXPORT_MAX_ROWS = 10_000


class TableInfo(BaseModel):
    schema_name: str
    table: str
    row_estimate: int | None
    total_bytes: int | None


class ColumnInfo(BaseModel):
    name: str
    type: str
    masked: bool


class RowsPage(BaseModel):
    schema_name: str
    table: str
    columns: list[ColumnInfo]
    rows: list[list[Any]]
    page: int
    page_size: int
    total: int | None
    latest_at: str | None


def _quote_ident(name: str) -> str:
    """Standard double-quote identifier escaping. Only ever applied to
    names read back from the DB catalog, never raw user input."""
    return '"' + name.replace('"', '""') + '"'


def _is_masked(column: str) -> bool:
    lowered = column.lower()
    return any(fragment in lowered for fragment in _MASK_FRAGMENTS)


def _is_postgres(db: AsyncSession) -> bool:
    bind = db.get_bind()
    return bind.dialect.name == "postgresql"


async def _catalog_tables(db: AsyncSession) -> list[TableInfo]:
    if _is_postgres(db):
        result = await db.execute(text(
            "SELECT n.nspname, c.relname, c.reltuples::bigint, "
            "       pg_total_relation_size(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname = ANY(:schemas) "
            "ORDER BY n.nspname, c.relname"
        ), {"schemas": list(SCHEMA_ALLOWLIST)})
        return [
            TableInfo(
                schema_name=r[0], table=r[1],
                row_estimate=max(int(r[2]), 0), total_bytes=int(r[3]),
            )
            for r in result.all()
        ]
    # SQLite (tests): one flat namespace presented as "public".
    result = await db.execute(text(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ))
    return [
        TableInfo(schema_name="public", table=r[0], row_estimate=None, total_bytes=None)
        for r in result.all()
    ]


async def _catalog_columns(
    db: AsyncSession, schema: str, table: str,
) -> list[tuple[str, str]] | None:
    """(name, type) per column from the catalog, or None when the table
    doesn't exist in the allowlisted schema."""
    if _is_postgres(db):
        result = await db.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "ORDER BY ordinal_position"
        ), {"schema": schema, "table": table})
        cols = [(r[0], r[1]) for r in result.all()]
        return cols or None
    exists = await db.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table"
    ), {"table": table})
    if exists.first() is None:
        return None
    # PRAGMA can't take bound params; `table` was just catalog-verified.
    result = await db.execute(text(f"PRAGMA table_info({_quote_ident(table)})"))
    return [(r[1], r[2] or "") for r in result.all()]


def _qualified(db: AsyncSession, schema: str, table: str) -> str:
    if _is_postgres(db):
        return f"{_quote_ident(schema)}.{_quote_ident(table)}"
    return _quote_ident(table)


async def _validated_query_parts(
    db: AsyncSession,
    schema: str,
    table: str,
    order_by: str | None,
    order_dir: str,
    filter_col: str | None,
    filter_op: str | None,
    filter_val: str | None,
) -> tuple[list[tuple[str, str]], str, str, str, dict[str, Any]]:
    """Validate every name against the catalog and assemble the shared
    SQL fragments: (columns, qualified, filter_sql, order_sql, params).
    Raises HTTPException on anything that doesn't check out."""
    if schema not in SCHEMA_ALLOWLIST:
        raise HTTPException(404, detail=f"schema {schema!r} not browsable")

    columns = await _catalog_columns(db, schema, table)
    if columns is None:
        raise HTTPException(404, detail=f"table {schema}.{table} not found")

    col_names = {name for name, _ in columns}

    if order_dir not in ("asc", "desc"):
        raise HTTPException(400, detail="order_dir must be asc|desc")
    if order_by is not None and order_by not in col_names:
        raise HTTPException(400, detail=f"unknown order_by column {order_by!r}")

    filter_sql = ""
    params: dict[str, Any] = {}
    if filter_col or filter_op or filter_val is not None:
        if not (filter_col and filter_op and filter_val is not None):
            raise HTTPException(400, detail="filter requires col, op and val together")
        if filter_col not in col_names:
            raise HTTPException(400, detail=f"unknown filter column {filter_col!r}")
        if filter_op not in _FILTER_OPS:
            raise HTTPException(400, detail=f"filter_op must be one of {sorted(_FILTER_OPS)}")
        sql_op = _FILTER_OPS[filter_op]
        if sql_op == "ILIKE" and not _is_postgres(db):
            sql_op = "LIKE"  # SQLite LIKE is case-insensitive for ASCII
        filter_sql = f" WHERE {_quote_ident(filter_col)} {sql_op} :filter_val"
        params["filter_val"] = filter_val

    # Always order: without it Postgres is free to return pages in any
    # order, so page 2 could repeat/skip rows. The catalog's first
    # column (ordinal 1 — usually the PK or timestamp) DESC is the
    # "newest first" default.
    effective_order = order_by or columns[0][0]
    order_sql = f" ORDER BY {_quote_ident(effective_order)} {order_dir.upper()}"

    return columns, _qualified(db, schema, table), filter_sql, order_sql, params


@router.get("/tables", response_model=list[TableInfo])
async def list_tables(_: AdminUser, db: DB):
    return await _catalog_tables(db)


@router.get("/tables/{schema}/{table}/rows", response_model=RowsPage)
async def table_rows(
    schema: str,
    table: str,
    user: AdminUser,
    db: DB,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1),
    order_by: str | None = Query(None),
    order_dir: str = Query("desc"),
    filter_col: str | None = Query(None),
    filter_op: str | None = Query(None),
    filter_val: str | None = Query(None),
):
    columns, qualified, filter_sql, order_sql, params = await _validated_query_parts(
        db, schema, table, order_by, order_dir, filter_col, filter_op, filter_val,
    )
    page_size = min(page_size, _MAX_PAGE_SIZE)

    if _is_postgres(db):
        await db.execute(text("SET LOCAL statement_timeout = '5000ms'"))

    select_list = ", ".join(_quote_ident(name) for name, _ in columns)
    result = await db.execute(text(
        f"SELECT {select_list} FROM {qualified}{filter_sql}{order_sql} "
        f"LIMIT :limit OFFSET :offset"
    ), {**params, "limit": page_size, "offset": (page - 1) * page_size})
    raw_rows = result.all()

    total: int | None
    try:
        total_result = await db.execute(
            text(f"SELECT count(*) FROM {qualified}{filter_sql}"), params,
        )
        total = int(total_result.scalar() or 0)
    except Exception:  # noqa: BLE001 — count timing out must not kill the page
        total = None

    latest_at: str | None = None
    ts_col = next((n for n, _ in columns if n in ("ingested_at", "updated_at")), None)
    if ts_col:
        try:
            latest_result = await db.execute(
                text(f"SELECT max({_quote_ident(ts_col)}) FROM {qualified}"),
            )
            latest_val = latest_result.scalar()
            latest_at = str(latest_val) if latest_val is not None else None
        except Exception:  # noqa: BLE001
            latest_at = None

    masked_idx = {i for i, (name, _) in enumerate(columns) if _is_masked(name)}
    rows = [
        [
            "***" if i in masked_idx and value is not None else _jsonable(value)
            for i, value in enumerate(row)
        ]
        for row in raw_rows
    ]

    log.info(
        "db_browser.query",
        extra={
            "admin_id": str(user.get("id", "")),
            "schema": schema,
            "table": table,
            "page": page,
            "filter_col": filter_col,
            "filter_op": filter_op,
        },
    )

    return RowsPage(
        schema_name=schema,
        table=table,
        columns=[
            ColumnInfo(name=name, type=type_, masked=_is_masked(name))
            for name, type_ in columns
        ],
        rows=rows,
        page=page,
        page_size=page_size,
        total=total,
        latest_at=latest_at,
    )


@router.get("/tables/{schema}/{table}/export.csv")
async def export_csv(
    schema: str,
    table: str,
    user: AdminUser,
    db: DB,
    order_by: str | None = Query(None),
    order_dir: str = Query("desc"),
    filter_col: str | None = Query(None),
    filter_op: str | None = Query(None),
    filter_val: str | None = Query(None),
):
    """CSV dump of the current view (same filter/order as the row
    endpoint), capped at _EXPORT_MAX_ROWS. Masking applies here too —
    export must not be a side door around it."""
    columns, qualified, filter_sql, order_sql, params = await _validated_query_parts(
        db, schema, table, order_by, order_dir, filter_col, filter_op, filter_val,
    )

    if _is_postgres(db):
        await db.execute(text("SET LOCAL statement_timeout = '5000ms'"))

    select_list = ", ".join(_quote_ident(name) for name, _ in columns)
    result = await db.execute(text(
        f"SELECT {select_list} FROM {qualified}{filter_sql}{order_sql} LIMIT :limit"
    ), {**params, "limit": _EXPORT_MAX_ROWS})
    raw_rows = result.all()

    log.info(
        "db_browser.export",
        extra={
            "admin_id": str(user.get("id", "")),
            "schema": schema,
            "table": table,
            "rows": len(raw_rows),
            "filter_col": filter_col,
            "filter_op": filter_op,
        },
    )

    masked_idx = {i for i, (name, _) in enumerate(columns) if _is_masked(name)}

    def _iter_csv():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([name for name, _ in columns])
        for row in raw_rows:
            writer.writerow([
                "***" if i in masked_idx and value is not None else _jsonable(value)
                for i, value in enumerate(row)
            ])
            if buf.tell() > 64 * 1024:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()

    filename = f"{schema}.{table}.csv"
    return StreamingResponse(
        _iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _jsonable(value: Any) -> Any:
    """Coerce driver types (Decimal, datetime, UUID, memoryview…) into
    JSON-friendly primitives; complex values fall back to str."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
