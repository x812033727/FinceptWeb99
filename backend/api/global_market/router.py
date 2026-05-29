"""International / cross-market news endpoints.

Backed by the `ingest_news_international` cron, which writes Chinese-
translated international financial coverage (US markets, Fed, FOMC,
global macro) into `news_articles` with `market='GLOBAL'`. Read path
is DB-only — there's no live RSS waterfall fallback the way the per-
symbol TW news endpoint has, because international news is intended
as discussion / dashboard context rather than a per-stock detail.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from dependencies import get_current_user

router = APIRouter()
CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get("/news/recent")
async def news_recent(_: CurrentUser, limit: int = Query(20, ge=1, le=50)):
    """Market-wide international news from the ingest archive. Returns
    `symbol IS NULL` rows under `market='GLOBAL'` plus sentiment_score /
    sentiment_label so the frontend can render coloured 利多/利空/中性
    badges. Empty list when the cron hasn't populated anything yet."""
    from services.ingest.repository import read_recent_news_autosession
    return await read_recent_news_autosession(
        "GLOBAL", symbol=None, limit=limit,
        max_age_days=7, include_sentiment=True,
    )


@router.get("/overseas-indicators")
async def overseas_indicators(_: CurrentUser):
    """Live snapshot of the curated overseas index universe (PR #270).

    Same data the discussion context uses (PR #269 ctx block), exposed
    as a public read so the DashboardPage can show overnight US
    direction at a glance — operators no longer have to open a
    discussion to see SOX / NDX / SPX / DJI / VIX moves.

    Returns `{as_of: null, indices: [...]}` (live mode only — backtest
    anchoring is internal to the discussion context). 5-min cached
    via the same Redis key as the ctx fetcher, so dashboard load
    doesn't fan out to yfinance unless the cache is cold.
    """
    from services.overseas_market_service import get_overseas_snapshot
    return await get_overseas_snapshot()
