"""FinMind clone API router.

Public endpoints (consumed by paying customers):
  GET  /api/finmind/data/{dataset_code}
       Mirrors FinMind's `api/v4/data` endpoint shape — same query
       params (data_id, start_date, end_date) plus `limit` cap.
  GET  /api/finmind/datasets
       Catalog discovery — list of available datasets, descriptions,
       freshness. No admin key needed; same shape FinMind exposes.

Admin endpoints (operator-only):
  GET    /api/finmind/admin/datasets
  PATCH  /api/finmind/admin/datasets/{dataset_code}
       Toggle enabled / flip active_source for Phase A → B transition.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.api.auth import require_admin_key, require_api_key
from finmind.api.schemas import (
    DataResponse,
    DataResponseMetadata,
    DatasetSourceItem,
    DatasetSourceUpdate,
)
from finmind.db.session import get_finmind_db
from finmind.models.dataset_source import DatasetSource

router = APIRouter()


# ── Public catalog discovery ─────────────────────────────────────


@router.get(
    "/datasets",
    response_model=list[DatasetSourceItem],
    summary="List every FinMind dataset and its current freshness",
)
async def list_datasets(
    db: AsyncSession = Depends(get_finmind_db),
    _api_key: str = Depends(require_api_key),
) -> list[DatasetSourceItem]:
    rows = (
        await db.execute(select(DatasetSource).order_by(DatasetSource.dataset_code))
    ).scalars().all()
    return [DatasetSourceItem.model_validate(r, from_attributes=True) for r in rows]


# ── Public data query ────────────────────────────────────────────


# Whitelist of columns that may appear in WHERE clauses. We do NOT
# accept arbitrary user-supplied column names because the local-
# table identifier is interpolated into raw SQL — every operator-
# facing column must be one of these well-known names.
_WHERE_COLUMNS = {"symbol", "ts", "stock_id", "contract", "cb_id"}


def _coerce_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


@router.get(
    "/data/{dataset_code}",
    response_model=DataResponse,
    summary="Query a FinMind dataset's local mirror by symbol + date range",
)
async def get_data(
    dataset_code: str,
    data_id: str | None = Query(None, description="Symbol / contract / cb_id."),
    start_date: str | None = Query(None, description="YYYY-MM-DD inclusive."),
    end_date: str | None = Query(None, description="YYYY-MM-DD inclusive."),
    limit: int = Query(10000, le=100000, ge=1),
    db: AsyncSession = Depends(get_finmind_db),
    _api_key: str = Depends(require_api_key),
) -> DataResponse:
    ds = await db.get(DatasetSource, dataset_code)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if not ds.local_table:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{dataset_code}: destination table not yet built "
                "(Phase 1 in progress for this dataset)"
            ),
        )

    where: list[str] = []
    params: dict[str, Any] = {}

    if data_id:
        # Per-symbol vs per-contract vs per-cb_id — try to match the
        # column shape to the table. PR-of-truth heuristic: prefer
        # `symbol` (per-symbol tables) and fall back to `contract`
        # (futures/options) or `cb_id` (CB tables) by inspection of
        # the table's column list.
        col = await _detect_id_column(db, ds.local_table)
        if col is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{dataset_code} ({ds.local_table}) has no per-row "
                    "identity column — drop `data_id` from your query"
                ),
            )
        where.append(f"{col} = :data_id")
        params["data_id"] = data_id

    sd = _coerce_date(start_date)
    ed = _coerce_date(end_date)
    if sd:
        where.append("ts >= :start_date")
        params["start_date"] = sd
    if ed:
        where.append("ts <= :end_date")
        params["end_date"] = ed

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    # `limit` is bound — Pydantic Query already constrained it to a
    # bounded int. Local table name is sourced from `dataset_sources`,
    # not user input, so direct interpolation is safe.
    sql = (
        f"SELECT * FROM {ds.local_table}{where_clause} "
        f"ORDER BY ts DESC LIMIT :limit_n"
    )
    params["limit_n"] = limit

    try:
        result = await db.execute(text(sql), params)
        rows = [dict(r._mapping) for r in result]
    except Exception as exc:
        # Likely a missing `ts` column on a non-time-series table
        # (broker_master, industry_chain). Surface a 400 rather than
        # a generic 500 so the customer can see "this dataset has no
        # ts axis" and adjust their query.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"query failed: {exc.__class__.__name__}: {str(exc)[:200]}",
        ) from exc

    return DataResponse(
        status=200,
        msg="success",
        data=rows,
        metadata=DataResponseMetadata(
            dataset_code=dataset_code,
            local_table=ds.local_table,
            active_source=ds.active_source,
            last_ingest_at=ds.last_ingest_at,
            row_count=len(rows),
        ),
    )


async def _detect_id_column(db: AsyncSession, table: str) -> str | None:
    """Pick the right identity column for a `data_id` filter.

    Looks at the table's columns once (per request) and prefers
    `symbol`, then `contract`, then `cb_id`. Returns None when none
    of those exist (e.g. market-wide aggregate tables) so the caller
    can return 400 instead of building a SELECT with an undefined
    column."""
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        result = await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": table},
        )
        cols = {r[0] for r in result}
    else:
        # SQLite-friendly path for tests.
        result = await db.execute(text(f"PRAGMA table_info({table})"))
        cols = {r[1] for r in result}

    for candidate in ("symbol", "contract", "cb_id"):
        if candidate in cols:
            return candidate
    return None


# ── Admin endpoints ──────────────────────────────────────────────


@router.get(
    "/admin/datasets",
    response_model=list[DatasetSourceItem],
    summary="Operator listing of every dataset_sources row",
)
async def admin_list_datasets(
    db: AsyncSession = Depends(get_finmind_db),
    _admin_key: str = Depends(require_admin_key),
) -> list[DatasetSourceItem]:
    rows = (
        await db.execute(select(DatasetSource).order_by(DatasetSource.dataset_code))
    ).scalars().all()
    return [
        DatasetSourceItem.model_validate(r, from_attributes=True)
        for r in rows
    ]


_VALID_SOURCES = {"finmind", "twse", "tpex", "taifex", "mops", "tdcc"}


@router.patch(
    "/admin/datasets/{dataset_code}",
    response_model=DatasetSourceItem,
    summary="Toggle enabled / flip active_source per dataset",
)
async def admin_update_dataset(
    dataset_code: str,
    body: DatasetSourceUpdate,
    db: AsyncSession = Depends(get_finmind_db),
    _admin_key: str = Depends(require_admin_key),
) -> DatasetSourceItem:
    row = await db.get(DatasetSource, dataset_code)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.active_source is not None:
        if body.active_source not in _VALID_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source must be one of {sorted(_VALID_SOURCES)}; "
                    f"got '{body.active_source}'"
                ),
            )
        row.active_source = body.active_source
    await db.commit()
    await db.refresh(row)
    return DatasetSourceItem.model_validate(row, from_attributes=True)
