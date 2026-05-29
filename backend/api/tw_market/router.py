from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated

from api.tw_market.schemas import (
    TWQuoteResponse,
    TWOHLCVBar,
    TWScreenerItem,
    TWIndexResponse,
)
from dependencies import get_current_user
import services.tw_market_service as svc

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get("/fundamentals/{symbol}")
async def fundamentals(symbol: str, _: CurrentUser):
    """本益比、股價淨值比、殖利率 from TWSE BWIBBU_d."""
    try:
        return await svc.get_fundamentals(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/quote/{symbol}", response_model=TWQuoteResponse)
async def quote(symbol: str, _: CurrentUser):
    try:
        return await svc.get_quote(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/history/{symbol}", response_model=list[TWOHLCVBar])
async def history(
    symbol: str,
    _: CurrentUser,
    months: int = Query(12, ge=1, le=60, description="Number of months of history"),
):
    try:
        return await svc.get_history(symbol, months=months)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/institutional/{symbol}", response_model=list[dict])
async def institutional(
    symbol: str,
    _: CurrentUser,
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
    _: CurrentUser,
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
    _: CurrentUser,
    months: int = Query(12, ge=1, le=36),
):
    """月營收 — monthly revenue with MoM and YoY growth rates."""
    try:
        return await svc.get_revenue(symbol, months=months)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/financials/{symbol}", response_model=list[dict])
async def financials(symbol: str, _: CurrentUser):
    """財報 (XBRL) via FinMind."""
    try:
        return await svc.get_financials(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/health/{symbol}")
async def health(symbol: str, _: CurrentUser, periods: int = Query(8, ge=1, le=20)):
    """財務體質 — derived margins, leverage, liquidity ratios with red/yellow/green lights."""
    try:
        return await svc.get_health(symbol, periods=periods)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/valuation-band/{symbol}")
async def valuation_band(
    symbol: str,
    _: CurrentUser,
    metric: str = Query("pe", pattern="^(pe|pb)$"),
    years: int = Query(5, ge=1, le=10),
):
    """估值帶 (PE / PB band) — daily series + mean / std / percentile stats."""
    try:
        return await svc.get_valuation_band(symbol, metric=metric, years=years)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/dividends/{symbol}", response_model=list[dict])
async def dividends(symbol: str, _: CurrentUser):
    """配息歷史 — works for both ordinary stocks and ETFs."""
    try:
        return await svc.get_dividends(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/etf/{symbol}/holdings")
async def etf_holdings(symbol: str, _: CurrentUser):
    """ETF 持股明細 — latest snapshot of constituents and weights."""
    try:
        return await svc.get_etf_holdings(symbol)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/screener", response_model=list[TWScreenerItem])
async def screener(
    _: CurrentUser,
    exchange: str | None = Query(None, description="TWSE | TPEx"),
    min_volume: int | None = Query(None, description="Minimum trading volume (shares)"),
    min_pe: float | None = Query(None, description="Minimum P/E ratio (本益比)"),
    max_pe: float | None = Query(None, description="Maximum P/E ratio (本益比)"),
    min_pb: float | None = Query(None, description="Minimum P/B ratio (股價淨值比)"),
    max_pb: float | None = Query(None, description="Maximum P/B ratio (股價淨值比)"),
    min_dividend_yield: float | None = Query(
        None, description="Minimum dividend yield % (殖利率)"
    ),
    include_etf: bool = Query(True, description="Include ETF symbols (00xxx)"),
    etf_only: bool = Query(False, description="Restrict results to ETFs only"),
    limit: int = Query(100, le=500),
):
    try:
        return await svc.get_screener(
            exchange=exchange,
            min_volume=min_volume,
            min_pe=min_pe,
            max_pe=max_pe,
            min_pb=min_pb,
            max_pb=max_pb,
            min_dividend_yield=min_dividend_yield,
            include_etf=include_etf,
            etf_only=etf_only,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/indices", response_model=TWIndexResponse)
async def indices(_: CurrentUser):
    """TAIEX 加權股價指數."""
    try:
        return await svc.get_index()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data source error: {e}")


@router.get("/industry/{symbol}")
async def industry(symbol: str, _: CurrentUser):
    """Return cached industry + name for a TW symbol.

    Backed by the in-memory `_industry_map` / `_name_map` populated
    daily by the `tw_symbol_map` cron from TWSE `t187ap03_L`. Empty
    fields when the symbol isn't recognised — frontend can fall
    back to "—" rather than 404 since the endpoint is informational
    enrichment, not a primary lookup.
    """
    sym = symbol.upper().strip()
    return {
        "symbol":   sym,
        "industry": svc.get_industry(sym),
        "name_zh":  svc.get_company_name(sym),
    }


@router.get("/news/recent")
async def news_recent(_: CurrentUser, limit: int = Query(20, ge=1, le=50)):
    """Market-wide TW news from the ingest archive — DB only, no live
    upstream fallback. Returns articles with `symbol IS NULL` (so per-
    symbol headlines don't crowd out broader market commentary) plus
    sentiment_score / sentiment_label for the dashboard's coloured
    badges. Empty list when the ingest task hasn't populated anything
    yet (fresh deploy, FinMind paywall window before PR #128, etc.)."""
    from services.ingest.repository import read_recent_news_autosession
    return await read_recent_news_autosession(
        "TW", symbol=None, limit=limit,
        max_age_days=7, include_sentiment=True,
    )


@router.get("/news/{symbol}")
async def news(symbol: str, _: CurrentUser, limit: int = Query(10, le=30)):
    """Recent news headlines for a TW stock (via yfinance .TW suffix)."""
    return await svc.get_news(symbol.upper(), limit=limit)


@router.get("/search")
async def search(
    _: CurrentUser,
    q: str = Query(..., min_length=1, max_length=20),
    limit: int = Query(10, ge=1, le=30),
):
    """Search TW exchange symbols by prefix or substring (case-insensitive)."""
    results = [
        {"symbol": sym, "market": "TW"}
        for sym in svc._exchange_map
        if q.upper() in sym.upper()
    ][:limit]
    return results
