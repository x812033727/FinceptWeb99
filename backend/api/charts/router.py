import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.charts.schemas import DrawingAlertCreate, DrawingCreate, DrawingOut, DrawingUpdate, validate_points
from auth.permissions import require_viewer
from db.session import get_db
from limiter import limiter
from models.alert import PriceAlert
from models.chart_drawing import ChartDrawing
from schemas.alert import AlertCreate, AlertOut
from services.alert_rules import TREND_CONDITION_TYPES

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


def _trend_params(points: list[dict]) -> dict:
    return {
        "start_time": points[0]["time"],
        "start_price": float(points[0]["price"]),
        "end_time": points[1]["time"],
        "end_price": float(points[1]["price"]),
    }


async def _owned(
    db: AsyncSession, drawing_id: uuid.UUID, user_id: str, *, lock: bool = False,
) -> ChartDrawing:
    statement = select(ChartDrawing).where(
        ChartDrawing.id == drawing_id,
        ChartDrawing.user_id == uuid.UUID(user_id),
    )
    if lock:
        statement = statement.with_for_update()
    drawing = await db.scalar(statement)
    if drawing is None:
        raise HTTPException(status_code=404, detail="Drawing not found")
    return drawing


@router.get("/{market}/{symbol}", response_model=list[DrawingOut])
async def list_drawings(
    market: Literal["US", "TW", "CRYPTO"], symbol: str, user: CurrentUser, db: DB,
):
    rows = await db.scalars(select(ChartDrawing).where(
        ChartDrawing.user_id == uuid.UUID(user["id"]),
        ChartDrawing.market == market,
        ChartDrawing.symbol == symbol.upper(),
    ).order_by(ChartDrawing.created_at))
    return list(rows)


@router.post("", response_model=DrawingOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("60/minute")
async def create_drawing(request: Request, body: DrawingCreate, user: CurrentUser, db: DB):
    drawing = ChartDrawing(user_id=uuid.UUID(user["id"]), **body.model_dump(mode="json"))
    db.add(drawing)
    await db.flush()
    await db.refresh(drawing)
    return drawing


@router.patch("/{drawing_id}", response_model=DrawingOut)
@limiter.limit("60/minute")
async def update_drawing(
    request: Request, drawing_id: uuid.UUID, body: DrawingUpdate, user: CurrentUser, db: DB,
):
    # Prevent two tabs from overwriting the same drawing concurrently.
    drawing = await _owned(db, drawing_id, user["id"], lock=True)
    fields = body.model_dump(exclude_unset=True, mode="json")
    if "points" in fields:
        from api.charts.schemas import DrawingPoint
        points = [DrawingPoint.model_validate(point) for point in fields["points"]]
        try:
            validate_points(drawing.kind, points)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    for key, value in fields.items():
        setattr(drawing, key, value)
    if "points" in fields and drawing.kind == "trend" and drawing.alert_id is not None:
        linked_alert = await db.scalar(select(PriceAlert).where(
            PriceAlert.id == drawing.alert_id,
            PriceAlert.user_id == uuid.UUID(user["id"]),
        ))
        if linked_alert is not None and linked_alert.condition_type in TREND_CONDITION_TYPES:
            # Keep a linked dynamic alert on the edited geometry. Clearing
            # state makes the next quote a baseline, never a false crossing.
            body_alert = AlertCreate(
                market=drawing.market,
                symbol=drawing.symbol,
                condition_type=linked_alert.condition_type,
                params=_trend_params(fields["points"]),
            )
            linked_alert.params = body_alert.params
            linked_alert.runtime_state = None
    drawing.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(drawing)
    return drawing


@router.delete("/{drawing_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_drawing(
    request: Request, drawing_id: uuid.UUID, user: CurrentUser, db: DB,
):
    drawing = await _owned(db, drawing_id, user["id"])
    await db.delete(drawing)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{drawing_id}/alert", response_model=AlertOut)
@limiter.limit("30/minute")
async def drawing_to_alert(
    request: Request, drawing_id: uuid.UUID, body: DrawingAlertCreate,
    user: CurrentUser, db: DB,
):
    # Alert creation and linking stay in one locked transaction, so two tabs
    # cannot create duplicate rules before either sees alert_id.
    drawing = await _owned(db, drawing_id, user["id"], lock=True)
    if drawing.alert_id is not None:
        existing = await db.scalar(select(PriceAlert).where(
            PriceAlert.id == drawing.alert_id,
            PriceAlert.user_id == uuid.UUID(user["id"]),
        ))
        if existing is not None:
            return existing
    if drawing.kind == "horizontal":
        body_alert = AlertCreate(
            market=drawing.market,
            symbol=drawing.symbol,
            condition_type=f"price_{body.condition}",
            target_price=float(drawing.points[0]["price"]),
            repeat=body.repeat,
            cooldown_seconds=body.cooldown_seconds,
        )
    else:
        body_alert = AlertCreate(
            market=drawing.market,
            symbol=drawing.symbol,
            condition_type=f"trend_cross_{body.condition}",
            params=_trend_params(drawing.points),
            repeat=body.repeat,
            cooldown_seconds=body.cooldown_seconds,
        )
    alert = PriceAlert(
        user_id=uuid.UUID(user["id"]), symbol=body_alert.symbol.upper(),
        market=body_alert.market, condition=body_alert.condition,
        target_price=body_alert.target_price, condition_type=body_alert.condition_type,
        params=body_alert.params, cooldown_seconds=body_alert.cooldown_seconds,
        repeat=body_alert.repeat,
    )
    db.add(alert)
    await db.flush()
    drawing.alert_id = alert.id
    drawing.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(alert)
    return alert
