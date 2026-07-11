import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from db.session import get_db
from limiter import limiter
from services.alert_service import AlertService

from .schemas import AlertCreate, AlertEventOut, AlertOut, AlertUpdate

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[AlertOut])
async def list_alerts(user: CurrentUser, db: DB):
    return await AlertService.list(db, uuid.UUID(user["id"]))


@router.get("/history", response_model=list[AlertEventOut])
async def alert_history(
    user: CurrentUser,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[datetime | None, Query()] = None,
):
    """Fired-alert history, newest first. Cursor pagination: pass the
    last row's `fired_at` as `before` to fetch the next (older) page."""
    return await AlertService.history(
        db, uuid.UUID(user["id"]), limit=limit, before=before,
    )


@router.post("", response_model=AlertOut, status_code=201)
@limiter.limit("30/minute")
async def create_alert(request: Request, body: AlertCreate, user: CurrentUser, db: DB):
    return await AlertService.create(db, uuid.UUID(user["id"]), body)


@router.patch("/{alert_id}", response_model=AlertOut)
@limiter.limit("30/minute")
async def update_alert(
    request: Request,
    alert_id: uuid.UUID,
    body: AlertUpdate,
    user: CurrentUser,
    db: DB,
):
    """Partial update of rule knobs (params / target_price /
    cooldown_seconds / repeat). params are re-validated against the
    alert's condition_type — mismatch → 422."""
    try:
        alert = await AlertService.update(db, uuid.UUID(user["id"]), alert_id, body)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not alert:
        raise HTTPException(404, "Alert not found")
    return alert


@router.delete("/{alert_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_alert(request: Request, alert_id: uuid.UUID, user: CurrentUser, db: DB):
    ok = await AlertService.delete(db, uuid.UUID(user["id"]), alert_id)
    if not ok:
        raise HTTPException(404, "Alert not found")
