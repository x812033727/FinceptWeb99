from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from models.alert import PriceAlert
from models.user import User
from models.watchlist import Watchlist

from ..schemas import SystemStats

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/stats", response_model=SystemStats)
async def stats(_: AdminUser, db: DB):
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
