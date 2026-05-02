"""Hourly TW news ingest.

Pulls TW market news from Google News RSS (zh-TW edition) — free, no
token, aggregates cnyes / 經濟日報 / 工商時報 / Yahoo TW / 鉅亨網 in one
call. Replaces FinMind's `TaiwanStockNews` (paid-only) which was
silently rejecting our free-tier token. The FinMind connector is kept
for other datasets (institutional / margin / revenue) that don't have
the same paywall.

Articles are deduplicated via sha256(normalized title + canonical link)
so re-running the task is idempotent — the next hourly tick won't re-
write yesterday's news. Fresh items get their per-symbol code regex'd
out of the title (4-6 digit token) so per-symbol sentiment queries
have something to read.

Failure handling:
  - HTTP errors are formatted with status code + actionable hint
    (`HTTP 429 (Google News rate-limit; back off)`, etc.) so operators
    see the cause without digging into logs.
  - Repeated failures arm an exponential backoff (1h → 2h → 4h → 6h
    cap) so the task stops hammering a known-bad upstream every cycle.
    A successful run clears the backoff state.
  - Backoff-skip ticks preserve the most recent real error in the
    health row so admins can see *why* the job is in cooldown without
    waiting for the window to expire.

Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import UTC, datetime

import httpx

import data.tw.google_news_tw_connector as google_news_tw
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

JOB_ID = "ingest_news_tw"

_LOCK_KEY = "lock:ingest_news_tw"
_LOCK_TTL = 5 * 60   # 5 min — one bulk call typically completes in seconds
_FETCH_LIMIT = 100   # Google News caps return at ~100 anyway


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
    """Turn a raw exception into a one-line operator-friendly summary.

    Google News RSS doesn't return a structured error body the way a
    JSON API does (it serves HTML or an error page on failure), so we
    lean on the HTTP status code + a static hint dict. Network /
    timeout / unknown errors fall back to a typed prefix + the
    exception message.
    """
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
        log.info("ingest_news_tw.skipped_lock_held")
        return
    try:
        # Backoff gate: if the previous run set a cooldown window we
        # silently skip work but still record health so operators see
        # the task is alive and counting down. Preserve the most
        # recent actual failure error in the new health entry — without
        # this, the IngestHealthCard masks the root cause behind a
        # generic "skipped" message and operators have to clear Redis
        # to see what's actually broken.
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            previous = await get_health(JOB_ID)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                # Trim to keep the health row readable; full context
                # is in the application logs.
                last_err = previous.error[:200]
                tail = f"; last: {last_err}"
            log.info(
                "ingest_news_tw.skipped_backoff",
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
            counters = await _do_run()
        except Exception as exc:
            detail = _format_news_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_news_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        # Success — clear any leftover backoff state from prior failures.
        await clear_failures(JOB_ID)
        log.info("ingest_news_tw.done", extra=counters)
        # `row_count` carries the row-count badge value (input rows that
        # made it past pubdate parsing). The status text exposes the
        # full counter set so admins can spot a parse-fail spike that
        # would otherwise look like "ok / 95 / 5m ago" with no signal
        # that 5 articles got dropped.
        status = (
            f"fetched={counters['fetched']} attempted={counters['attempted']}"
        )
        if counters["dropped_pubdate"]:
            status += f" dropped_pubdate={counters['dropped_pubdate']}"
        await record_health(
            JOB_ID, ok=True, row_count=counters["attempted"], error=status,
        )
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> dict[str, int]:
    """Pull, dedupe, persist. Returns counters for the health record.

    Raises on hard failure (HTTP error, network, etc.) — the caller
    catches and arms backoff. Empty results are treated as success
    with `attempted=0` ("RSS responded but parsed to nothing").

    Counters surface to the admin UI so a parse-fail spike (Google
    changing pubDate format) doesn't silently degrade to "ok / 0".
    """
    items = await google_news_tw.get_news(limit=_FETCH_LIMIT)
    fetched = len(items)
    if not items:
        return {"fetched": 0, "attempted": 0, "dropped_pubdate": 0}

    rows: list[NewsArticleRow] = []
    dropped_pubdate = 0
    for item in items:
        # Distinguish "title/link missing" (silently drop, normal) from
        # "pubdate unparseable" (count + log, abnormal). _to_row returns
        # None for both, so re-check before the count.
        if not (item.get("title") or "").strip() or not (item.get("link") or "").strip():
            continue
        row = _to_row(item)
        if row is None:
            dropped_pubdate += 1
            continue
        rows.append(row)

    async with AsyncSessionLocal() as db:
        await insert_news_articles(db, rows)
    return {
        "fetched": fetched,
        "attempted": len(rows),
        "dropped_pubdate": dropped_pubdate,
    }


def _to_row(item: dict) -> NewsArticleRow | None:
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title or not link:
        return None

    published_raw = item.get("published_at") or ""
    try:
        # Connector returns ISO 8601 UTC ("...+00:00") but tolerates
        # mis-parsed pubDate strings flowing through unchanged. Cover
        # both cases plus the date-only form. Articles that still fail
        # to parse here are *dropped* (return None) rather than stamped
        # with `datetime.now(UTC)` — the old fallback poisoned the
        # archive: a parse-fail article would carry an "ingestion-time"
        # timestamp forever, so backtest mode (`gather_market_context
        # (as_of=...)`) saw it in every window and the resulting
        # `news_sentiment` looked indistinguishable from live mode.
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
        log.info(
            "ingest_news_tw.pubdate_parse_failed",
            extra={"raw": published_raw[:80], "title": title[:80]},
        )
        return None

    return NewsArticleRow(
        market="TW",
        # Two-stage symbol tagging (PR #215):
        #   1. Connector's 4-6 digit regex on the title (catches
        #      "**2330** 法說會優於預期")
        #   2. Fallback to in-memory name map on the title — catches
        #      "**台積電**法說會超預期 - 經濟日報" which has no
        #      digit code. Without this, ~half of Taiwanese stock
        #      headlines slip through with `symbol=NULL` and never
        #      surface in `per_symbol_news_sentiment` lookups.
        # Both lookups are pure in-memory; cost is negligible.
        symbol=item.get("symbol") or _name_fallback_symbol(title),
        published_at=published_at,
        title=title,
        link=link,
        publisher=item.get("source_name") or None,
        summary=(item.get("description") or "").strip() or None,
        payload=None,
        source="google_news_tw",
    )


def _name_fallback_symbol(title: str) -> str | None:
    """Defer the import so the task module loads even if
    `tw_market_service` raises during startup (its `_name_map`
    population talks to TWSE; on first boot before the symbol-map
    cron has run, calling here just returns None — graceful)."""
    try:
        from services.tw_market_service import find_symbol_by_name_in_text
        return find_symbol_by_name_in_text(title)
    except Exception:
        return None
