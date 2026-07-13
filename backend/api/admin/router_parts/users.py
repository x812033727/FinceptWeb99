import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from models.user import User, UserRole

from ..schemas import ActiveUpdate, AdminUserItem, RoleUpdate

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]

VALID_ROLES = {r.value for r in UserRole}


@router.get("/users", response_model=list[AdminUserItem])
async def list_users(
    _: AdminUser,
    db: DB,
    offset: int = 0,
    limit: int = 50,
):
    rows = await db.scalars(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    return list(rows.all())


@router.patch("/users/{user_id}/role", status_code=204)
async def update_role(user_id: uuid.UUID, body: RoleUpdate, _: AdminUser, db: DB):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {VALID_ROLES}")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.role = UserRole(body.role)
    await db.commit()


@router.patch("/users/{user_id}/active", status_code=204)
async def update_active(user_id: uuid.UUID, body: ActiveUpdate, _: AdminUser, db: DB):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = body.is_active
    await db.commit()
