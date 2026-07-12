"""Backfill `news_articles.body` with extracted full text (G5 phase 2).

Runs periodically, decoupled from ingest so slow page fetches don't
hold up the hourly feed pull. Each run:

  1. picks up to `_PER_RUN_CAP` recent direct-feed articles that haven't
     had an extraction attempt (`body_fetched_at IS NULL`),
  2. fetches + extracts each via `services.news_fulltext.extract_body`
     (robots-respecting, per-domain throttled),
  3. stamps `body_fetched_at` on every attempt (body may stay NULL) so a
     dead page is never retried.

Only sources with `fulltext_enabled` are scoped in — Google-News rows
(redirect links) are never touched.
"""
import logging

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    read_news_needing_body,
    record_health,
    update_news_body,
)
from services.news_fulltext import extract_body
from services.news_sources import fulltext_source_keys

log = logging.getLogger(__name__)

JOB_ID = "enrich_news_fulltext"
_LOCK_KEY = "lock:enrich_news_fulltext"
_LOCK_TTL = 20 * 60
_PER_RUN_CAP = 40


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("enrich_news_fulltext.skipped_lock_held")
        return
    try:
        try:
            filled, attempted = await _do_run()
        except Exception as exc:
            log.warning("enrich_news_fulltext.failed", extra={"error": str(exc)})
            await record_health(
                JOB_ID, ok=False, row_count=0, error=f"unexpected: {exc}",
            )
            return
        log.info("enrich_news_fulltext.done",
                 extra={"filled": filled, "attempted": attempted})
        # row_count = bodies actually extracted this run.
        await record_health(JOB_ID, ok=True, row_count=filled)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> tuple[int, int]:
    """Returns (bodies_filled, articles_attempted)."""
    keys = fulltext_source_keys()
    if not keys:
        return 0, 0

    async with AsyncSessionLocal() as db:
        targets = await read_news_needing_body(
            db, source_keys=keys, limit=_PER_RUN_CAP,
        )

    filled = 0
    for article_id, link in targets:
        body = await extract_body(link)
        # Separate session per update keeps each attempt durable even if
        # a later fetch hangs; the fetches are the slow part, not the DB.
        async with AsyncSessionLocal() as db:
            await update_news_body(db, article_id, body)
        if body:
            filled += 1
    return filled, len(targets)
