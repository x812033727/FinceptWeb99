from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Annotated

from api.us_market.schemas import (
    QuoteResponse,
    OHLCVBar,
    FundamentalsResponse,
    FinancialsResponse,
    ScreenerItem,
    MacroDataPoint,
)
from dependencies import get_current_user
from limiter import limiter
import services.us_market_service as svc

router = APIRouter()
Auth = Annotated[dict, Depends(get_current_user)]


@router.get("/quote/{ticker}", response_model=QuoteResponse)
async def quote(ticker: str, _: Auth):
    try:
        return await svc.get_quote(ticker.upper())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/history/{ticker}", response_model=list[OHLCVBar])
async def history(
    ticker: str,
    _: Auth,
    period: str = Query("1y", description="1d 5d 1mo 3mo 6mo 1y 2y 5y 10y"),
    interval: str = Query("1d", description="1m 5m 15m 1h 1d 1wk 1mo"),
):
    try:
        return await svc.get_history(ticker.upper(), period=period, interval=interval)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/fundamentals/{ticker}", response_model=FundamentalsResponse)
async def fundamentals(ticker: str, _: Auth):
    try:
        data = await svc.get_fundamentals(ticker.upper())
        return FundamentalsResponse(
            symbol=data["symbol"],
            market=data["market"],
            name=data.get("name"),
            sector=data.get("sector"),
            industry=data.get("industry"),
            market_cap=data.get("market_cap"),
            pe_ratio=data.get("pe_ratio"),
            pb_ratio=data.get("pb_ratio"),
            eps=data.get("eps"),
            dividend_yield=data.get("dividend_yield"),
            beta=data.get("beta"),
            fifty_two_week_high=data.get("52w_high"),
            fifty_two_week_low=data.get("52w_low"),
            description=data.get("description"),
            fetched_at=data["fetched_at"],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/financials/{ticker}", response_model=FinancialsResponse)
async def financials(ticker: str, _: Auth):
    try:
        data = await svc.get_financials(ticker.upper())
        return FinancialsResponse(symbol=ticker.upper(), **data)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/options/{ticker}", response_model=list[dict])
async def options(
    ticker: str,
    _: Auth,
    expiration_date: str | None = Query(None, description="YYYY-MM-DD"),
):
    try:
        return await svc.get_options(ticker.upper(), expiration_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/screener", response_model=list[ScreenerItem])
@limiter.limit("30/minute")
async def screener(
    request: Request,
    _: Auth,
    min_market_cap: float | None = Query(None, description="Minimum market cap in USD"),
    max_pe: float | None = Query(None, description="Maximum P/E ratio"),
    min_volume: int | None = Query(None, description="Minimum daily volume"),
    sector: str | None = Query(None, description="Sector filter (partial match)"),
    limit: int = Query(100, le=500),
):
    try:
        return await svc.get_screener(
            min_market_cap=min_market_cap,
            max_pe=max_pe,
            min_volume=min_volume,
            sector=sector,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/macro/{indicator}", response_model=list[MacroDataPoint])
async def macro(indicator: str, _: Auth):
    """
    Available indicators: fed_funds_rate, unemployment, cpi, gdp,
    10y_yield, 2y_yield, 10y_minus_2y, usd_index, twd_usd
    """
    data = await svc.get_macro_indicator(indicator)
    if not data:
        raise HTTPException(status_code=404, detail="Indicator not found or FRED key not configured")
    return data


@router.get("/news/{ticker}")
async def news(ticker: str, _: Auth, limit: int = Query(10, le=30)):
    """Recent news headlines for a US stock (via yfinance)."""
    return await svc.get_news(ticker.upper(), limit=limit)


@router.get("/earnings/{ticker}")
async def earnings(ticker: str, _: Auth):
    """Next earnings date and EPS/revenue consensus estimate for a US stock."""
    return await svc.get_earnings(ticker.upper())


@router.get("/search")
async def search(
    _: Auth,
    q: str = Query(..., min_length=1, max_length=20),
    limit: int = Query(10, ge=1, le=30),
):
    """Search S&P 500 symbols by prefix or substring (case-insensitive)."""
    q_upper = q.upper()
    tickers = await svc._get_sp500_tickers()
    results = [t for t in tickers if q_upper in t][:limit]
    return [{"symbol": t, "market": "US"} for t in results]
