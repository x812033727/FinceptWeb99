"""Paper-trading order lifecycle endpoints."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from dependencies import get_current_user
from limiter import limiter
from services import paper_trading_service as svc

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]
DB = Annotated[AsyncSession, Depends(get_db)]


class PaperOrderCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    market: Literal["US", "TW", "CRYPTO"]
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    time_in_force: Literal["day", "gtc"] = "day"
    quantity: float = Field(..., gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    reference_price: float | None = Field(default=None, gt=0)
    fee_bps: float = Field(default=0, ge=0, le=10_000)
    idempotency_key: str = Field(..., min_length=8, max_length=120)
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_reservation_price(self):
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required for a limit order")
        if self.order_type == "market" and self.reference_price is None:
            raise ValueError("reference_price is required for a market order")
        return self


class PaperFillCreate(BaseModel):
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    idempotency_key: str = Field(..., min_length=8, max_length=120)
    filled_at: datetime | None = None


class PaperOrderResponse(BaseModel):
    id: UUID
    portfolio_id: UUID
    symbol: str
    market: str
    side: str
    order_type: str
    time_in_force: str
    quantity: float
    filled_quantity: float
    limit_price: float | None
    reservation_price: float
    average_fill_price: float | None
    fee_bps: float
    status: str
    idempotency_key: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None
    expires_at: datetime | None
    expired_at: datetime | None

    model_config = {"from_attributes": True}


class PaperFillResponse(BaseModel):
    id: UUID
    order_id: UUID
    transaction_id: UUID
    quantity: float
    price: float
    fee: float
    quote_price: float | None
    slippage_bps: float | None
    liquidity_quantity: float | None
    quote_key: str | None
    execution_source: str
    idempotency_key: str
    filled_at: datetime

    model_config = {"from_attributes": True}


def _error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message.lower():
        return HTTPException(status_code=404, detail=message)
    if isinstance(exc, svc.PaperTradingConflict):
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


@router.post(
    "/{portfolio_id}/paper-orders",
    response_model=PaperOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("60/minute")
async def submit_paper_order(
    request: Request,
    portfolio_id: str,
    body: PaperOrderCreate,
    user: CurrentUser,
    db: DB,
):
    try:
        return await svc.submit_order(
            portfolio_id=portfolio_id,
            user_id=user["id"],
            db=db,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise _error(exc)


@router.get("/{portfolio_id}/paper-orders", response_model=list[PaperOrderResponse])
async def list_paper_orders(
    portfolio_id: str,
    user: CurrentUser,
    db: DB,
    limit: int = Query(default=200, ge=1, le=1000),
):
    try:
        return await svc.list_orders(
            portfolio_id=portfolio_id,
            user_id=user["id"],
            db=db,
            limit=limit,
        )
    except ValueError as exc:
        raise _error(exc)


@router.get("/{portfolio_id}/paper-orders/{order_id}", response_model=PaperOrderResponse)
async def get_paper_order(
    portfolio_id: str,
    order_id: str,
    user: CurrentUser,
    db: DB,
):
    try:
        return await svc.get_order(
            portfolio_id=portfolio_id,
            order_id=order_id,
            user_id=user["id"],
            db=db,
        )
    except ValueError as exc:
        raise _error(exc)


@router.get(
    "/{portfolio_id}/paper-orders/{order_id}/fills",
    response_model=list[PaperFillResponse],
)
async def list_paper_fills(portfolio_id: str, order_id: str, user: CurrentUser, db: DB):
    try:
        return await svc.list_fills(
            portfolio_id=portfolio_id,
            order_id=order_id,
            user_id=user["id"],
            db=db,
        )
    except ValueError as exc:
        raise _error(exc)


@router.post(
    "/{portfolio_id}/paper-orders/{order_id}/fills",
    response_model=PaperFillResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("120/minute")
async def fill_paper_order(
    request: Request,
    portfolio_id: str,
    order_id: str,
    body: PaperFillCreate,
    user: CurrentUser,
    db: DB,
):
    try:
        return await svc.fill_order(
            portfolio_id=portfolio_id,
            order_id=order_id,
            user_id=user["id"],
            db=db,
            **body.model_dump(),
        )
    except ValueError as exc:
        raise _error(exc)


@router.post(
    "/{portfolio_id}/paper-orders/{order_id}/cancel",
    response_model=PaperOrderResponse,
)
@limiter.limit("60/minute")
async def cancel_paper_order(
    request: Request,
    portfolio_id: str,
    order_id: str,
    user: CurrentUser,
    db: DB,
):
    try:
        return await svc.cancel_order(
            portfolio_id=portfolio_id,
            order_id=order_id,
            user_id=user["id"],
            db=db,
        )
    except ValueError as exc:
        raise _error(exc)


@router.post(
    "/{portfolio_id}/paper-orders/{order_id}/match",
    response_model=PaperFillResponse | None,
)
@limiter.limit("120/minute")
async def match_paper_order(
    request: Request,
    portfolio_id: str,
    order_id: str,
    user: CurrentUser,
    db: DB,
):
    from services import paper_matching_service as matching

    try:
        return await matching.match_order(
            portfolio_id=portfolio_id,
            order_id=order_id,
            user_id=user["id"],
            db=db,
        )
    except ValueError as exc:
        raise _error(exc)
