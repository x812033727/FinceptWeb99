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


def _format_status(result: dict) -> str:
    """Render `score_pending`'s counters into a single-line status the
    admin UI's IngestHealthCard can show alongside the row_count.

    Distinguishes the three "scored=0" cases that look identical on the
    badge alone:
      - nothing in DB to score      → `considered=0 (nothing pending)`
      - LLM daily cap reached       → `considered=N cap_hit (daily LLM limit)`
      - LLM returned unparseable    → `considered=N scored=0 (LLM output unparseable)`
    Successful runs read as `considered=N scored=N batches=B`.
    """
    considered = int(result.get("considered", 0))
    scored = int(result.get("scored", 0))
    batches = int(result.get("batches", 0))
    cap_hit = bool(result.get("cap_hit"))

    if considered == 0:
        return "considered=0 (nothing pending — DB has no unscored news)"
    if cap_hit:
        return (
            f"considered={considered} scored={scored} cap_hit "
            "(SENTIMENT_DAILY_LLM_CALL_CAP reached; resumes UTC 00:00)"
        )
    if scored == 0:
        return (
            f"considered={considered} scored=0 "
            "(LLM output unparseable — check provider key + model)"
        )
    return f"considered={considered} scored={scored} batches={batches}"


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("score_news_sentiment.skipped_lock_held")
        return
    try:
        result = await score_pending()
        log.info("score_news_sentiment.done", extra=result)
        await record_health(
            JOB_ID, ok=True, row_count=int(result.get("scored", 0)),
            error=_format_status(result),
        )
    except Exception as exc:
        log.exception("score_news_sentiment.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)
