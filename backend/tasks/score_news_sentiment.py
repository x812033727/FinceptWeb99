"""Hourly news-sentiment scoring task.

Picks up news rows whose `sentiment_scored_at` is NULL and asks an LLM to
label each. See `services/news_sentiment_service.py` for batching and
prompt details.

Multi-pod safe via Redis SET-NX lock — without it, two replicas would race
to score the same rows and double-charge the LLM provider.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from services.ingest.repository import record_health
from services.news_sentiment_service import score_pending

log = logging.getLogger(__name__)

JOB_ID = "score_news_sentiment"

_LOCK_KEY = "lock:score_news_sentiment"
_LOCK_TTL = 10 * 60   # 10 min — a full pass of 4 batches × 20 rows fits easily


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("score_news_sentiment.skipped_lock_held")
        return
    try:
        result = await score_pending()
        log.info("score_news_sentiment.done", extra=result)
        await record_health(
            JOB_ID, ok=True, row_count=result.get("scored", 0),
        )
    except Exception as exc:
        log.exception("score_news_sentiment.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)
