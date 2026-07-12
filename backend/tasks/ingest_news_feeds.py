"""Hourly ingest of DIRECT publisher RSS feeds (G5 news enhancement).

Complements `ingest_news_tw` (Google News aggregator) by polling each
first-party feed registered in `services.news_sources`. Every article
flows through the SAME `insert_news_articles` path, so dedup
(`sha256(title+link)`) automatically collapses an article that appears
both here and via Google News.

Two things this task does better than the Google-News path:
  * First-party links — the article URL points at the publisher, not a
    Google redirect, so the future full-text extractor can reach the body.
  * Dictionary-first symbol tagging — the company-name map is the PRIMARY
    tagger (a headline naming 台積電 → 2330 even with no digit code),
    with the 4-6 digit regex only as a fallback. The Google-News path
    does the reverse (regex first), missing ~half of name-only headlines.

One dead feed doesn't sink the run: per-source fetch failures are logged
and skipped; the job only records an unhealthy result if EVERY enabled
source failed (a real outage, not one flaky publisher).
"""
import logging
import re
from datetime import UTC, datetime

import httpx

from cache.redis_cache import acquire_lock, release_lock
from data.rss_connector import fetch_feed
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    NewsArticleRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    insert_news_articles,
    record_failure,
    record_health,
)
from services.news_sources import enabled_sources

log = logging.getLogger(__name__)

JOB_ID = "ingest_news_feeds"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_news_feeds"
_LOCK_TTL = 10 * 60
_PER_FEED_LIMIT = 60

# Fallback digit-code tagger (secondary to the name dictionary). Same
# 4-6 digit shape as the Google-News connector.
_TW_SYMBOL_RE = re.compile(r"\b(\d{4,6})\b")


def _tag_symbol(title: str) -> str | None:
    """Dictionary-first symbol tagging. Company-name longest-match wins;
    a bare digit code is the fallback (headlines lead with the name far
    more often than the code)."""
    try:
        from services.tw_market_service import find_symbol_by_name_in_text
        hit = find_symbol_by_name_in_text(title)
        if hit:
            return hit
    except Exception:
        # Name map not warm yet (first boot before symbol-map cron) —
        # fall through to the regex.
        pass
    m = _TW_SYMBOL_RE.search(title or "")
    return m.group(1) if m else None


def _to_row(item: dict, source_key: str) -> NewsArticleRow | None:
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title or not link:
        return None
    try:
        published_at = datetime.fromisoformat(item["published_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    else:
        published_at = published_at.astimezone(UTC)

    return NewsArticleRow(
        market=MARKET,
        symbol=_tag_symbol(title),
        published_at=published_at,
        title=title,
        link=link,
        publisher=None,
        summary=(item.get("description") or "").strip() or None,
        payload=None,
        source=source_key,
    )


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase or '?'}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout: {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_news_feeds.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            previous = await get_health(JOB_ID)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                tail = f"; last: {previous.error[:200]}"
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(f"skipped (backoff after {failures} failures, "
                       f"~{mins} min remaining{tail})"),
            )
            return

        try:
            row_count = await _do_run()
        except Exception as exc:
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning("ingest_news_feeds.failed",
                        extra={"error": detail, "failures": failures})
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info("ingest_news_feeds.done", extra={"rows_processed": row_count})
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    """Fetch every enabled TW feed, map to rows, bulk-insert (dedup on
    conflict). Raises only when EVERY feed failed — one flaky publisher
    is logged and skipped, not fatal.

    Records a PER-SOURCE health entry (`newsfeed:<key>`) for each feed so
    the admin health card shows which of the 4 independent publishers is
    alive / failing — a job-level "ok" would hide one silently-dead feed
    among three healthy ones."""
    sources = enabled_sources(market=MARKET)
    if not sources:
        return 0

    all_rows: list[NewsArticleRow] = []
    failures = 0
    for src in sources:
        try:
            items = await fetch_feed(src.url, limit=_PER_FEED_LIMIT)
        except Exception as exc:
            failures += 1
            detail = _format_error(exc)
            log.warning("ingest_news_feeds.feed_failed",
                        extra={"source": src.key, "error": detail})
            await record_health(
                f"newsfeed:{src.key}", ok=False, row_count=0, error=detail,
            )
            continue
        rows = [r for it in items if (r := _to_row(it, src.key)) is not None]
        all_rows.extend(rows)
        # row_count = items this feed returned (pre-dedup) — the health
        # signal is "feed alive + returning articles", not net-new inserts.
        await record_health(f"newsfeed:{src.key}", ok=True, row_count=len(rows))

    if failures == len(sources):
        # Every feed failed — real outage, arm backoff.
        raise RuntimeError(f"all {failures} news feeds failed")

    if not all_rows:
        return 0

    async with AsyncSessionLocal() as db:
        return await insert_news_articles(db, all_rows)
