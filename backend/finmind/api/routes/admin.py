"""Admin endpoints for the FinMind clone — operator-gated by
`FINMIND_ADMIN_API_KEY`. Three routes:

  GET    /admin/datasets       — full dataset_sources list
  PATCH  /admin/datasets/{code} — toggle enabled / flip active_source
  GET    /admin/usage           — per-day / per-dataset / per-key rollup

The PATCH refuses to flip `active_source` to a stub connector via
`is_source_implemented` so an operator can't break the cron silently
(see PR #306).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func as sa_func
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.api.auth import require_admin_key
from finmind.api.schemas import DatasetSourceItem, DatasetSourceUpdate
from finmind.db.session import get_finmind_db
from finmind.models.dataset_source import DatasetSource

router = APIRouter()


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
        # Same two-layer gatekeeper as the main-app proxy
        # (api/admin/finmind_proxy.py): the outer allowlist comes from
        # known_sources() (registry-derived, can't drift), then
        # is_source_implemented() rejects stub sources and
        # covers_dataset() rejects flips to a real connector that lacks
        # a handler for THIS dataset — either would break ingest at
        # runtime with NotImplementedError.
        from finmind.ingest.selfcrawl import (
            covers_dataset,
            is_source_implemented,
            known_sources,
        )

        valid_sources = known_sources()
        if body.active_source not in valid_sources:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source must be one of {sorted(valid_sources)}; "
                    f"got '{body.active_source}'"
                ),
            )
        if not is_source_implemented(body.active_source):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source='{body.active_source}' has no "
                    f"connector wired up yet — implement "
                    f"finmind/ingest/selfcrawl/{body.active_source}.py "
                    f"+ register_connector() before flipping."
                ),
            )
        if not covers_dataset(body.active_source, dataset_code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source='{body.active_source}' has a connector "
                    f"but no handler for dataset '{dataset_code}'. Add a "
                    f"handler to finmind/ingest/selfcrawl/"
                    f"{body.active_source}.py:_DISPATCH before flipping, OR "
                    f"keep the current active_source for this dataset."
                ),
            )
        row.active_source = body.active_source
    await db.commit()
    await db.refresh(row)
    return DatasetSourceItem.model_validate(row, from_attributes=True)


@router.get(
    "/admin/usage",
    summary="Per-key + per-dataset usage rollup over the last N days",
)
async def admin_usage(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_finmind_db),
    _admin_key: str = Depends(require_admin_key),
) -> dict[str, Any]:
    """Powers the AdminPage UsageCard chart. Returns three rollups:

      - `by_day`:     calls + rows per UTC day across all keys
      - `by_dataset`: calls + rows per dataset_code (last N days)
      - `by_key`:     calls + rows per api_key_id (last N days,
                      NULL key_id rolled up as `<anonymous>`)

    All three derived from the same window. Cheap — `api_usage_events`
    is a hypertable + the queries hit indexed (api_key_id, ts) /
    indexed (ts) paths."""
    from finmind.models.billing import ApiUsageEvent

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    by_day_rows = (
        await db.execute(
            select(
                sa_func.strftime("%Y-%m-%d", ApiUsageEvent.ts).label("day")
                if db.bind.dialect.name == "sqlite"
                else sa_func.to_char(ApiUsageEvent.ts, "YYYY-MM-DD").label("day"),
                sa_func.count().label("calls"),
                sa_func.coalesce(sa_func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(sa_text("day"))
            .order_by(sa_text("day"))
        )
    ).all()

    by_dataset_rows = (
        await db.execute(
            select(
                ApiUsageEvent.dataset_code,
                sa_func.count().label("calls"),
                sa_func.coalesce(sa_func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(ApiUsageEvent.dataset_code)
            .order_by(sa_func.count().desc())
        )
    ).all()

    by_key_rows = (
        await db.execute(
            select(
                ApiUsageEvent.api_key_id,
                sa_func.count().label("calls"),
                sa_func.coalesce(sa_func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(ApiUsageEvent.api_key_id)
            .order_by(sa_func.count().desc())
        )
    ).all()

    return {
        "window_days": days,
        "by_day": [
            {"day": r[0], "calls": int(r[1]), "rows": int(r[2])}
            for r in by_day_rows
        ],
        "by_dataset": [
            {
                "dataset_code": r[0] or "<unknown>",
                "calls": int(r[1]),
                "rows": int(r[2]),
            }
            for r in by_dataset_rows
        ],
        "by_key": [
            {
                "api_key_id": r[0],
                "owner_label": (
                    f"key#{r[0]}" if r[0] is not None else "<anonymous>"
                ),
                "calls": int(r[1]),
                "rows": int(r[2]),
            }
            for r in by_key_rows
        ],
    }
