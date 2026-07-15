"""Discussion daily auto-run config sub-router.

Owner-scoped read / upsert of the per-user daily auto-run config that
drives the scheduled discussion job. Quota-free — no AI credits are
reserved or refunded here, so these endpoints don't depend on the
`_refund` binding tests patch on `api.discussion.router`.

Mounted under `/api/discussion` so paths keep the `/api/discussion/...`
shape the frontend already calls — identical to before the split.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import CurrentUser, _coerce_owner_uuid
from api.discussion.schemas import AutoRunConfigRequest, AutoRunConfigResponse
from db.session import get_db
from services import discussion_auto_run_config_service

router = APIRouter()


def _counts(row=None):
    return discussion_auto_run_config_service.normalize_strategy_run_counts(
        getattr(row, "strategy_run_counts", None),
        legacy_enabled=bool(getattr(row, "enabled", False)),
    )


@router.get("/auto-run/config", response_model=AutoRunConfigResponse)
async def get_auto_run_config(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Read the current user's daily auto-run config. Returns a row of
    sensible defaults (`enabled=false`, empty topic / rules / personas)
    if the user has never saved one — the UI can render an empty form
    without a 404 dance."""
    row = await discussion_auto_run_config_service.get_config(
        db, user_id=_coerce_owner_uuid(user),
    )
    if row is None:
        return AutoRunConfigResponse(
            enabled=False,
            persona_ids=[],
            topic="",
            rules="",
            market="TW",
            send_email=False,
            strategy_run_counts=_counts(),
            updated_at=None,
        )
    return AutoRunConfigResponse(
        enabled=row.enabled,
        persona_ids=list(row.persona_ids or []),
        topic=row.topic,
        rules=row.rules,
        market=row.market,
        send_email=bool(row.send_email),
        updated_at=row.updated_at,
        strategy_run_counts=_counts(row),
    )


@router.put("/auto-run/config", response_model=AutoRunConfigResponse)
async def put_auto_run_config(
    body: AutoRunConfigRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        row = await discussion_auto_run_config_service.upsert_config(
            db,
            user_id=_coerce_owner_uuid(user),
            enabled=body.enabled,
            persona_ids=body.persona_ids,
            topic=body.topic,
            rules=body.rules,
            market=body.market,
            send_email=body.send_email,
            strategy_run_counts=body.strategy_run_counts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AutoRunConfigResponse(
        enabled=row.enabled,
        persona_ids=list(row.persona_ids or []),
        topic=row.topic,
        rules=row.rules,
        market=row.market,
        send_email=bool(row.send_email),
        updated_at=row.updated_at,
        strategy_run_counts=_counts(row),
    )
