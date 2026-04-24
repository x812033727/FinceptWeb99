from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated

from api.tw_market.schemas import (
    TWQuoteResponse,
    TWOHLCVBar,
    InstitutionalRow,
    MarginRow,
    RevenueRow,
    TWScreenerItem,
    TWIndexResponse,
)
from dependencies import get_current_user
import services.tw_market_service as svc

router = APIRouter()
Auth = Annotated[dict, Depends(get_current_user)]


@router.get("/quote/{symbol}", response_model=TWQuoteResponse)
async def quote(symbol: str, _: Auth):
    try:
        return await svc.get_quote(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/history/{symbol}", response_model=list[TWOHLCVBar])
async def history(
    symbol: str,
    _: Auth,
    months: int = Query(12, ge=1, le=60, description="Number of months of history"),
):
    try:
        return await svc.get_history(symbol, months=months)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/institutional/{symbol}", response_model=list[dict])
async def institutional(
    symbol: str,
    _: Auth,
    days: int = Query(30, ge=1, le=365),
):
    """法人買賣超 — foreign investors, investment trusts, dealers."""
    try:
        return await svc.get_institutional(symbol, days=days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/margin/{symbol}", response_model=list[dict])
async def margin(
    symbol: str,
    _: Auth,
    days: int = Query(30, ge=1, le=365),
):
    """融資融券 — margin purchase and short sale balances."""
    try:
        return await svc.get_margin(symbol, days=days)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/revenue/{symbol}", response_model=list[dict])
async def revenue(
    symbol: str,
    _: Auth,
    months: int = Query(12, ge=1, le=36),
):
    """月營收 — monthly revenue with MoM and YoY growth rates."""
    try:
        return await svc.get_revenue(symbol, months=months)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/financials/{symbol}", response_model=list[dict])
async def financials(symbol: str, _: Auth):
    """財報 (XBRL) via FinMind."""
    try:
        return await svc.get_financials(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/screener", response_model=list[TWScreenerItem])
async def screener(
    _: Auth,
    exchange: str | None = Query(None, description="TWSE | TPEx"),
    min_volume: int | None = Query(None, description="Minimum trading volume (shares)"),
    limit: int = Query(100, le=500),
):
    try:
        return await svc.get_screener(exchange=exchange, min_volume=min_volume, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/indices", response_model=TWIndexResponse)
async def indices(_: Auth):
    """TAIEX 加權股價指數."""
    try:
        return await svc.get_index()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")
