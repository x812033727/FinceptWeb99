"""Hourly TW news ingest.

Pulls market-wide news from FinMind (`TaiwanStockNews` with empty
`data_id` returns the cross-section in one call — no per-symbol
fan-out, ~24 calls/day, far below FinMind's 600/day quota). Articles
are deduplicated via sha256(normalized title + canonical link) so
re-running the task is idempotent.

The market-wide pull captures both per-symbol articles (FinMind tags
each row with `stock_id`) and unattached headlines. The repository's
read path filters by symbol when present, by IS NULL when absent.

Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import UTC, datetime, timedelta

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    NewsArticleRow,
    insert_news_articles,
    record_health,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_news_tw"

_LOCK_KEY = "lock:ingest_news_tw"
_LOCK_TTL = 5 * 60   # 5 min — one bulk call typically completes in seconds
_LOOKBACK_DAYS = 2   # FinMind returns articles published within range; 2 days
                     # tolerates timezone offsets and overlap with prior runs


async def run() -> None:
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_news_tw.skipped_lock_held")
        return
    try:
        await _do_run()
    except Exception as exc:
        log.exception("ingest_news_tw.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> None:
    start = (datetime.now(UTC).date() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    try:
        items = await finmind.get_news(start_date=start)
    except Exception as exc:
        log.warning("ingest_news_tw.finmind_failed", extra={"error": str(exc)})
        await record_health(JOB_ID, ok=False, error=f"finmind_unavailable: {exc}")
        return

    if not items:
        # Empty can mean (a) no new news in window or (b) FinMind quota
        # exhausted (returns []). Either way we record a health entry so
        # operators see the run executed.
        log.info("ingest_news_tw.empty_result")
        await record_health(JOB_ID, ok=True, row_count=0)
        return

    rows = [r for r in (_to_row(it) for it in items) if r is not None]

    async with AsyncSessionLocal() as db:
        written = await insert_news_articles(db, rows)

    log.info("ingest_news_tw.done", extra={"rows_processed": written})
    await record_health(JOB_ID, ok=True, row_count=written)


def _to_row(item: dict) -> NewsArticleRow | None:
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title or not link:
        return None

    published_raw = item.get("published_at") or ""
    try:
        # FinMind returns "YYYY-MM-DD HH:MM:SS" (UTC+8). Treat as UTC+8
        # naive then convert to UTC for storage.
        if "T" in published_raw:
            published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        elif " " in published_raw:
            published_at = datetime.fromisoformat(published_raw)
        else:
            published_at = datetime.fromisoformat(published_raw + "T00:00:00")
        if published_at.tzinfo is None:
            # FinMind times are already in Asia/Taipei. Subtract 8h to
            # store as UTC.
            published_at = published_at.replace(tzinfo=UTC) - timedelta(hours=8)
    except (TypeError, ValueError):
        published_at = datetime.now(UTC)

    return NewsArticleRow(
        market="TW",
        symbol=item.get("symbol"),
        published_at=published_at,
        title=title,
        link=link,
        publisher=item.get("source_name") or None,
        summary=(item.get("description") or "").strip() or None,
        payload=None,
        source="finmind",
    )
