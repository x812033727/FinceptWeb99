"""Hourly news + announcement sentiment scoring task.

Picks up rows from `news_articles` AND `corporate_announcements`
whose `sentiment_scored_at` is NULL and asks an LLM to label each.
See `services/news_sentiment_service.py` for batching and prompt
details.

The two lanes share a single daily LLM-call budget
(`SENTIMENT_DAILY_LLM_CALL_CAP`) — important because at 100 calls/day
default, both archives easily fit but a heavy news backlog used to
crowd out the announcements lane. Lane order is news-first because
the news archive is bigger (~10× row count) and benefits more from
draining; announcements run with whatever cap remains. If the cap
is hit mid-news, announcements simply skip this tick and try again
next hour.

Multi-pod safe via Redis SET-NX lock — without it, two replicas
would race to score the same rows and double-charge the LLM provider.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from services.ingest.repository import record_health
from services.news_sentiment_service import (
    score_pending,
    score_pending_announcements,
)

log = logging.getLogger(__name__)

JOB_ID = "score_news_sentiment"

_LOCK_KEY = "lock:score_news_sentiment"
_LOCK_TTL = 10 * 60   # 10 min — a full pass of 4 batches × 20 rows fits easily


def _format_lane(label: str, result: dict) -> str:
    """One-lane summary in the format `news=considered/scored
    (batches)`. Hides cap_hit + error per-lane because the combined
    status surfaces them at the end.
    """
    considered = int(result.get("considered", 0))
    scored = int(result.get("scored", 0))
    batches = int(result.get("batches", 0))
    return f"{label}={considered}/{scored} ({batches}b)"


def _format_combined_status(news: dict, ann: dict) -> str:
    """Build a single-line IngestHealthCard status from both lanes.

    Shape: `news=N/S (Bb) ann=N/S (Bb)` plus any cap-hit / error
    suffix. Examples:
      - `news=20/20 (1b) ann=8/8 (1b)`            (healthy)
      - `news=80/0 (4b) ann=0/0 (0b) (LLM ...)`   (news LLM unparseable)
      - `news=80/40 (2b) ann=0/0 (0b) cap_hit`    (news ate the cap)
    """
    parts = [_format_lane("news", news), _format_lane("ann", ann)]
    cap_hit = bool(news.get("cap_hit") or ann.get("cap_hit"))
    if cap_hit:
        parts.append("cap_hit (daily LLM limit)")
    err = news.get("error") or ann.get("error")
    if err:
        parts.append(f"err={str(err)[:120]}")
    return " ".join(parts)


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("score_news_sentiment.skipped_lock_held")
        return
    try:
        # News first (bigger backlog, benefits more from draining
        # remaining cap). Both lanes consult the same Redis counter
        # via `_can_make_llm_call`, so announcements only see what
        # the news pass left behind.
        news_result = await score_pending()
        ann_result = await score_pending_announcements()
        log.info(
            "score_news_sentiment.done",
            extra={"news": news_result, "announcements": ann_result},
        )
        total_scored = (
            int(news_result.get("scored", 0))
            + int(ann_result.get("scored", 0))
        )
        await record_health(
            JOB_ID, ok=True, row_count=total_scored,
            error=_format_combined_status(news_result, ann_result),
        )
    except Exception as exc:
        log.exception("score_news_sentiment.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)
