import uuid
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from models.alert import PriceAlert
from models.user import User, UserRole
from models.watchlist import Watchlist
from services.version_service import trigger_update
from .schemas import ActiveUpdate, AdminUserItem, RoleUpdate, SystemStats, UpdateResult

router = APIRouter()
Admin = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]

VALID_ROLES = {r.value for r in UserRole}


@router.get("/stats", response_model=SystemStats)
async def stats(_: Admin, db: DB):
    total_users = await db.scalar(select(func.count(User.id)))
    active_users = await db.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))
    )
    by_role_rows = await db.execute(
        select(User.role, func.count(User.id)).group_by(User.role)
    )
    users_by_role = {row[0].value: row[1] for row in by_role_rows}

    total_alerts = await db.scalar(select(func.count(PriceAlert.id)))
    total_watchlists = await db.scalar(select(func.count(Watchlist.id)))

    return SystemStats(
        total_users=total_users or 0,
        active_users=active_users or 0,
        users_by_role=users_by_role,
        total_alerts=total_alerts or 0,
        total_watchlists=total_watchlists or 0,
    )


@router.get("/users", response_model=list[AdminUserItem])
async def list_users(
    _: Admin,
    db: DB,
    offset: int = 0,
    limit: int = 50,
):
    rows = await db.scalars(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    return list(rows.all())


@router.patch("/users/{user_id}/role", status_code=204)
async def update_role(user_id: uuid.UUID, body: RoleUpdate, _: Admin, db: DB):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {VALID_ROLES}")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.role = UserRole(body.role)
    await db.commit()


@router.patch("/users/{user_id}/active", status_code=204)
async def update_active(user_id: uuid.UUID, body: ActiveUpdate, _: Admin, db: DB):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = body.is_active
    await db.commit()


@router.post("/update", response_model=UpdateResult)
async def trigger_system_update(_: Admin) -> UpdateResult:
    result = await trigger_update()
    return UpdateResult(**result)
