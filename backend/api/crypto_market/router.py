"""Crypto market endpoints — Kraken-backed spot quote / history / screener.

Public Kraken API; no API key required. All quotes returned in USD; the
service layer treats USDT/USDC/DAI as USD when computing portfolio value.
"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from dependencies import get_current_user
import services.crypto_market_service as svc

router = APIRouter()
Auth = Annotated[dict, Depends(get_current_user)]


@router.get("/quote/{symbol}")
async def quote(symbol: str, _: Auth) -> dict[str, Any]:
    data = await svc.get_quote(symbol.upper())
    if data.get("price") is None:
        raise HTTPException(status_code=404, detail=data.get("error", "symbol not found"))
    return data


@router.get("/history/{symbol}")
async def history(
    symbol: str,
    _: Auth,
    interval: str = Query("1d", description="1m 5m 15m 30m 1h 4h 1d 1w"),
    limit: int = Query(365, ge=1, le=720),
) -> list[dict[str, Any]]:
    return await svc.get_history(symbol.upper(), interval=interval, limit=limit)


@router.get("/screener")
async def screener(
    _: Auth,
    limit: int = Query(20, ge=1, le=20),
) -> list[dict[str, Any]]:
    return await svc.get_screener(limit=limit)


@router.get("/search")
async def search(
    _: Auth,
    q: str = Query(..., min_length=1, max_length=20),
    limit: int = Query(10, ge=1, le=20),
) -> list[dict[str, Any]]:
    return await svc.search(q, limit=limit)
