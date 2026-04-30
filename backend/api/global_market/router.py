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
Auth = Annotated[dict, Depends(get_current_user)]


@router.get("/news/recent")
async def news_recent(_: Auth, limit: int = Query(20, ge=1, le=50)):
    """Market-wide international news from the ingest archive. Returns
    `symbol IS NULL` rows under `market='GLOBAL'` plus sentiment_score /
    sentiment_label so the frontend can render coloured 利多/利空/中性
    badges. Empty list when the cron hasn't populated anything yet."""
    from services.ingest.repository import read_recent_news_autosession
    return await read_recent_news_autosession(
        "GLOBAL", symbol=None, limit=limit,
        max_age_days=7, include_sentiment=True,
    )
