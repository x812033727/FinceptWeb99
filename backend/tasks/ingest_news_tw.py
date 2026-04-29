"""Hourly TW news ingest.

Pulls market-wide news from FinMind (`TaiwanStockNews` with empty
`data_id` returns the cross-section in one call — no per-symbol
fan-out, ~24 calls/day, far below FinMind's 600/day quota). Articles
are deduplicated via sha256(normalized title + canonical link) so
re-running the task is idempotent.

The market-wide pull captures both per-symbol articles (FinMind tags
each row with `stock_id`) and unattached headlines. The repository's
read path filters by symbol when present, by IS NULL when absent.

Failure handling:
  - HTTP errors are formatted with status code + actionable hint
    (`HTTP 401 (check FINMIND_TOKEN)`, `HTTP 402 (dataset paid only)`,
    `HTTP 429 (quota exhausted)`) so operators see the cause without
    digging into logs.
  - Repeated failures arm an exponential backoff (1h → 2h → 4h → 6h
    cap) so the task stops hammering a known-bad upstream every cycle.
    A successful run clears the backoff state.

Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import UTC, datetime, timedelta

import httpx

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    NewsArticleRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    insert_news_articles,
    record_failure,
    record_health,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_news_tw"

_LOCK_KEY = "lock:ingest_news_tw"
_LOCK_TTL = 5 * 60   # 5 min — one bulk call typically completes in seconds
_LOOKBACK_DAYS = 2   # FinMind returns articles published within range; 2 days
                     # tolerates timezone offsets and overlap with prior runs


# ── error formatting ──────────────────────────────────────────────


_HTTP_HINTS: dict[int, str] = {
    401: "check FINMIND_TOKEN — invalid or missing",
    402: "FinMind dataset requires paid sponsorship",
    403: "FinMind token forbidden for this dataset",
    429: "FinMind quota exhausted — resets at UTC 00:00",
    500: "FinMind upstream error",
    502: "FinMind bad gateway",
    503: "FinMind unavailable",
    504: "FinMind gateway timeout",
}


def _format_finmind_error(exc: BaseException) -> str:
    """Turn a raw exception into a one-line operator-friendly summary.

    Common HTTP statuses get a hint string suggesting the most likely
    cause + remediation. Network / timeout / unknown errors fall back
    to a typed prefix + the exception message so the source is still
    obvious.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = exc.response.reason_phrase or "?"
        hint = _HTTP_HINTS.get(code, "")
        body_msg = ""
        try:
            body = exc.response.json()
            for key in ("msg", "message", "detail"):
                if isinstance(body, dict) and body.get(key):
                    body_msg = f" — {body[key]}"
                    break
        except Exception:
            pass
        suffix = f" ({hint})" if hint else ""
        return f"HTTP {code} {reason}{suffix}{body_msg}"
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
        # the task is alive and counting down.
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            log.info(
                "ingest_news_tw.skipped_backoff",
                extra={"failures": failures, "seconds_remaining": remaining},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining)"
                ),
            )
            return

        try:
            row_count = await _do_run()
        except Exception as exc:
            detail = _format_finmind_error(exc)
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
        log.info("ingest_news_tw.done", extra={"rows_processed": row_count})
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    """Pull, dedupe, persist. Returns row count.

    Raises on hard failure (HTTP error, network, etc.) — the caller
    catches and arms backoff. Empty results are treated as success
    with row_count=0 (means "FinMind responded but no fresh articles").
    """
    start = (datetime.now(UTC).date() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_news(start_date=start)

    if not items:
        # FinMind returned [] either because no fresh news in the window
        # or because we hit our local quota guard. Both are non-fatal:
        # don't bump the failure counter, just record the empty pass.
        return 0

    rows = [r for r in (_to_row(it) for it in items) if r is not None]
    async with AsyncSessionLocal() as db:
        written = await insert_news_articles(db, rows)
    return written


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
