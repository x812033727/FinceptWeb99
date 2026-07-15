import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from db.session import get_db
from services.weekly_research_summary_service import build_weekly_summary
from api.research.schemas import LatestStockPickRuns, StockPickRunOut
from services.daily_pick_service import (
    NoEligibleCandidatesError,
    generate_daily_picks,
    latest_pick_runs,
    list_pick_runs,
)

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/weekly-summary")
async def weekly_summary(user: CurrentUser, db: DB):
    return await build_weekly_summary(db, uuid.UUID(user["id"]))


@router.post(
    "/daily-picks/generate",
    response_model=StockPickRunOut,
    status_code=status.HTTP_201_CREATED,
)
async def generate_picks(
    user: CurrentUser,
    db: DB,
    market: str = Query(pattern="^(TW|US)$"),
):
    try:
        return await generate_daily_picks(db, uuid.UUID(user["id"]), market)
    except NoEligibleCandidatesError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/daily-picks/latest", response_model=LatestStockPickRuns)
async def latest_picks(user: CurrentUser, db: DB):
    return LatestStockPickRuns(runs=await latest_pick_runs(db, uuid.UUID(user["id"])))


@router.get("/daily-picks", response_model=list[StockPickRunOut])
async def pick_history(
    user: CurrentUser,
    db: DB,
    limit: int = Query(default=20, ge=1, le=100),
):
    return await list_pick_runs(db, uuid.UUID(user["id"]), limit=limit)
