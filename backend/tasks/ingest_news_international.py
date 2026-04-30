"""Hourly international financial news ingest.

Pulls Chinese-translated international financial news from Google News
RSS (zh-TW edition) — same connector + locale as the TW pipeline, but
with a query targeted at international topics (US markets, Fed, macro
indicators) and stored under `market='GLOBAL'`. Aggregates TW media's
translated coverage of Bloomberg / Reuters / WSJ etc., so personas
that speak Chinese can reason about international macro context
without hitting an English-language source.

Articles are deduplicated via sha256(normalized title + canonical link)
so re-running the task is idempotent. Per-article symbol tags are
explicitly stripped: international headlines might contain incidental
4-digit tokens (years, percentages over 1000, etc.) that the TW
connector's symbol regex would mis-tag as TW stock codes — that would
poison `read_symbol_sentiment` lookups when a discussion mentioned
e.g. "2026" in a topic.

Failure handling and backoff mirror `tasks/ingest_news_tw.py` —
exponential 1h → 6h cap, error preservation across the cooldown window.

Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import UTC, datetime

import httpx

import data.tw.google_news_tw_connector as google_news
from cache.redis_cache import acquire_lock, release_lock
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

log = logging.getLogger(__name__)

JOB_ID = "ingest_news_international"
MARKET = "GLOBAL"

_LOCK_KEY = "lock:ingest_news_international"
_LOCK_TTL = 5 * 60
_FETCH_LIMIT = 100

# Chinese-translated international finance coverage. `OR` is a Google
# News operator (case-sensitive). Tuning this is a code change rather
# than a runtime knob — sentiment scoring and discussion context both
# read downstream of it.
_QUERY = "美股 OR 聯準會 OR FOMC OR 國際財經 OR 全球經濟"


# ── error formatting ──────────────────────────────────────────────


_HTTP_HINTS: dict[int, str] = {
    400: "Google News rejected the request — query may be malformed",
    403: "Google News refused — UA blocked or geo-restricted",
    429: "Google News rate-limit — backoff and retry later",
    500: "Google News upstream error",
    502: "Google News bad gateway",
    503: "Google News unavailable",
    504: "Google News gateway timeout",
}


def _format_news_error(exc: BaseException) -> str:
    """One-line operator-friendly summary. Mirrors `ingest_news_tw`."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = exc.response.reason_phrase or "?"
        hint = _HTTP_HINTS.get(code, "")
        suffix = f" ({hint})" if hint else ""
        return f"HTTP {code} {reason}{suffix}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"connect failed: {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


# ── orchestration ─────────────────────────────────────────────────


async def run() -> None:
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_news_international.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            previous = await get_health(JOB_ID)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                last_err = previous.error[:200]
                tail = f"; last: {last_err}"
            log.info(
                "ingest_news_international.skipped_backoff",
                extra={"failures": failures, "seconds_remaining": remaining},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining{tail})"
                ),
            )
            return

        try:
            row_count = await _do_run()
        except Exception as exc:
            detail = _format_news_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_news_international.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_news_international.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    """Pull, dedupe, persist. Returns row count."""
    items = await google_news.get_news(query=_QUERY, limit=_FETCH_LIMIT)

    if not items:
        return 0

    rows = [r for r in (_to_row(it) for it in items) if r is not None]
    async with AsyncSessionLocal() as db:
        written = await insert_news_articles(db, rows)
    return written


def _to_row(item: dict) -> NewsArticleRow | None:
    """Map a Google News RSS item to a `NewsArticleRow`. Symbol is
    explicitly None — see module docstring for why we don't reuse the
    TW connector's regex for international titles.
    """
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title or not link:
        return None

    published_raw = item.get("published_at") or ""
    try:
        if "T" in published_raw:
            published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        elif " " in published_raw:
            published_at = datetime.fromisoformat(published_raw)
        else:
            published_at = datetime.fromisoformat(published_raw + "T00:00:00")
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        else:
            published_at = published_at.astimezone(UTC)
    except (TypeError, ValueError):
        published_at = datetime.now(UTC)

    return NewsArticleRow(
        market=MARKET,
        symbol=None,
        published_at=published_at,
        title=title,
        link=link,
        publisher=item.get("source_name") or None,
        summary=(item.get("description") or "").strip() or None,
        payload=None,
        source="google_news_intl",
    )
