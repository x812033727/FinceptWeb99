from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import services.intraday_service as intraday_svc
import services.us_market_service as svc
from api.market_data_quality import build_quality_meta
from api.us_market.schemas import (
    FinancialsResponse,
    FundamentalsResponse,
    MacroDataPoint,
    OHLCVBar,
    OptionsAnalysisResponse,
    QuoteResponse,
    ScreenerItem,
)
from dependencies import get_current_user
from limiter import limiter

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get("/quote/{ticker}", response_model=QuoteResponse)
async def quote(
    ticker: str,
    _: CurrentUser,
    verify: bool = Query(False, description="Cross-check against an independent provider"),
):
    try:
        data = await svc.get_quote(ticker.upper())
        if verify:
            data["quality_check"] = await svc.verify_quote_consistency(ticker.upper(), data)
        data["meta"] = build_quality_meta(
            data, kind="quote",
            fallback_chain=["polygon", "yfinance", "stooq", "finnhub"],
        ).model_dump()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/history/{ticker}", response_model=list[OHLCVBar])
async def history(
    ticker: str,
    _: CurrentUser,
    period: str = Query("1y", description="1d 5d 1mo 3mo 6mo 1y 2y 5y 10y"),
    interval: str = Query("1d", description="1m 5m 15m 1h 1d 1wk 1mo"),
):
    try:
        bars = await svc.get_history(ticker.upper(), period=period, interval=interval)
        anchor = bars[-1].get("time") if bars else None
        return [
            {**bar, "meta": build_quality_meta(
                bar, kind="history", as_of=anchor,
                fallback_chain=["polygon", "yfinance"],
            ).model_dump()}
            for bar in bars
        ]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/intraday/{ticker}", response_model=intraday_svc.IntradayResponse)
async def intraday(
    ticker: str,
    _: CurrentUser,
    interval: str = Query("5m", pattern="^(1m|5m|15m)$", description="1m 5m 15m"),
):
    """分時 K 線 — aggregated from the quote_snapshots archive, limited to
    the snapshot retention window (`coverage_days`). Empty `bars` when the
    symbol has no snapshots — expected, not an error."""
    return await intraday_svc.get_intraday("US", ticker, interval)


@router.get("/fundamentals/{ticker}", response_model=FundamentalsResponse)
async def fundamentals(ticker: str, _: CurrentUser):
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
            data_source=data.get("data_source", "unavailable"),
            meta=build_quality_meta(
                data, kind="fundamentals",
                fallback_chain=["polygon", "yfinance"],
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/financials/{ticker}", response_model=FinancialsResponse)
async def financials(ticker: str, _: CurrentUser):
    try:
        data = await svc.get_financials(ticker.upper())
        return FinancialsResponse(symbol=ticker.upper(), **data)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/options/{ticker}", response_model=list[dict])
async def options(
    ticker: str,
    _: CurrentUser,
    expiration_date: str | None = Query(None, description="YYYY-MM-DD"),
):
    try:
        return await svc.get_options(ticker.upper(), expiration_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/options-analysis/{ticker}", response_model=OptionsAnalysisResponse)
@limiter.limit("30/minute")
async def options_analysis(
    request: Request,
    ticker: str,
    _: CurrentUser,
    max_expiries: int = Query(8, ge=1, le=12),
):
    """Derived chain analytics with explicit completeness and methodology."""
    from services.options_analytics_service import get_options_analysis

    try:
        return await get_options_analysis(ticker.upper(), max_expiries=max_expiries)
    except Exception as exc:
        # Provider exceptions may embed request URLs containing API keys;
        # keep the public error stable. Exception chaining remains available
        # to configured server-side error reporting without entering JSON.
        raise HTTPException(status_code=502, detail="Options analysis unavailable") from exc


@router.get("/screener", response_model=list[ScreenerItem])
@limiter.limit("30/minute")
async def screener(
    request: Request,
    _: CurrentUser,
    min_market_cap: float | None = Query(None, description="Minimum market cap in USD"),
    min_pe: float | None = Query(None, description="Minimum P/E ratio"),
    max_pe: float | None = Query(None, description="Maximum P/E ratio"),
    min_pb: float | None = Query(None, description="Minimum P/B ratio"),
    max_pb: float | None = Query(None, description="Maximum P/B ratio"),
    min_dividend_yield: float | None = Query(None, description="Minimum dividend yield % (e.g. 3 = 3%)"),
    min_volume: int | None = Query(None, description="Minimum daily volume"),
    sector: str | None = Query(None, description="Sector filter (partial match)"),
    limit: int = Query(100, le=500),
):
    try:
        return await svc.get_screener(
            min_market_cap=min_market_cap,
            min_pe=min_pe,
            max_pe=max_pe,
            min_pb=min_pb,
            max_pb=max_pb,
            min_dividend_yield=min_dividend_yield,
            min_volume=min_volume,
            sector=sector,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/macro/{indicator}", response_model=list[MacroDataPoint])
async def macro(indicator: str, _: CurrentUser):
    """
    Available indicators: fed_funds_rate, unemployment, cpi, gdp,
    10y_yield, 2y_yield, 10y_minus_2y, usd_index, twd_usd
    """
    data = await svc.get_macro_indicator(indicator)
    if not data:
        raise HTTPException(status_code=404, detail="Indicator not found or FRED key not configured")
    return data


@router.get("/news/{ticker}")
async def news(ticker: str, _: CurrentUser, limit: int = Query(10, le=30)):
    """Recent news headlines for a US stock (via yfinance)."""
    rows = await svc.get_news(ticker.upper(), limit=limit)
    return [
        {**row, "meta": build_quality_meta(
            row, kind="dataset",
            as_of=row.get("published_at"),
            fallback_chain=["google_news", "yfinance"],
        ).model_dump()}
        for row in rows
    ]


@router.get("/earnings/{ticker}")
async def earnings(ticker: str, _: CurrentUser):
    """Next earnings date and EPS/revenue consensus estimate for a US stock."""
    return await svc.get_earnings(ticker.upper())


@router.get("/search")
async def search(
    _: CurrentUser,
    q: str = Query(..., min_length=1, max_length=20),
    limit: int = Query(10, ge=1, le=30),
):
    """Search S&P 500 symbols by prefix or substring (case-insensitive)."""
    q_upper = q.upper()
    tickers = await svc._get_sp500_tickers()
    results = [t for t in tickers if q_upper in t][:limit]
    return [{"symbol": t, "market": "US"} for t in results]
