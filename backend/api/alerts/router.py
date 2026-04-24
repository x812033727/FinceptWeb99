import uuid
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from db.session import get_db
from services.alert_service import AlertService
from .schemas import AlertCreate, AlertOut

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("", response_model=list[AlertOut])
async def list_alerts(user: CurrentUser, db: DB):
    return await AlertService.list(db, uuid.UUID(user["id"]))


@router.post("", response_model=AlertOut, status_code=201)
async def create_alert(body: AlertCreate, user: CurrentUser, db: DB):
    return await AlertService.create(db, uuid.UUID(user["id"]), body)


@router.delete("/{alert_id}", status_code=204)
async def delete_alert(alert_id: uuid.UUID, user: CurrentUser, db: DB):
    ok = await AlertService.delete(db, uuid.UUID(user["id"]), alert_id)
    if not ok:
        raise HTTPException(404, "Alert not found")
