"""Hourly US 8-K filings ingest (PR-D3).

Sister cron to `ingest_announcements_tw` but pulls SEC EDGAR's
recent 8-K filings ATOM feed instead of MOPS. Writes into the
shared `corporate_announcements` table with `market='US'`, so:

  - The same hourly `score_news_sentiment` cron picks up the
    unscored US rows on its next pass (D1b's two-lane scorer
    handles arbitrary markets — no per-market dispatch needed
    inside the scorer).
  - The same `corporate_announcements` ctx block surfaces them
    when the discussion's market is 'US' (D3 also removes the
    TW-only early-return from the ctx block).

Cadence: hourly. Even though SEC's filing window is 4 business
days, the RSS feed updates within minutes of a filing, and 8-K
material events lose actionability fast (earnings beats / misses
get priced in within an hour). 30-min would burn double the SEC
courtesy budget for marginal freshness; daily would lag the news
cycle. Hourly is the sweet spot.

Failure handling mirrors `ingest_news_tw` exactly — exponential
backoff on repeated failure, last-error preservation in the health
row. Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import UTC, datetime

import httpx

import data.us.sec_edgar_connector as sec_edgar
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    CorporateAnnouncementRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    insert_corporate_announcements,
    record_failure,
    record_health,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_announcements_us"

_LOCK_KEY = "lock:ingest_announcements_us"
_LOCK_TTL = 5 * 60   # 5 min — bulk RSS call typically completes in 1-3s
_FETCH_LIMIT = 100   # SEC RSS caps return at 100 anyway


_HTTP_HINTS: dict[int, str] = {
    400: "SEC rejected the request — endpoint may have changed",
    403: "SEC refused — User-Agent rejected (set SEC_EDGAR_USER_AGENT_EMAIL)",
    429: "SEC rate-limit — backoff and retry later (10 req/sec is the cap)",
    500: "SEC upstream error",
    502: "SEC bad gateway",
    503: "SEC unavailable (often during scheduled maintenance windows)",
    504: "SEC gateway timeout",
}


def _format_sec_error(exc: BaseException) -> str:
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


async def run() -> None:
    """APScheduler entry point."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_announcements_us.skipped_lock_held")
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
            log.info(
                "ingest_announcements_us.skipped_backoff",
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
            detail = _format_sec_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_announcements_us.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info("ingest_announcements_us.done", extra=counters)
        status = (
            f"fetched={counters['fetched']} attempted={counters['attempted']}"
        )
        if counters["err_count"]:
            status += f" parse_errors={counters['err_count']}"
        await record_health(
            JOB_ID, ok=True, row_count=counters["attempted"], error=status,
        )
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> dict[str, int]:
    """Pull SEC EDGAR, parse, dedup, persist.

    Counters returned for the health record:
      - `fetched`     — rows the connector returned (post-parse +
                        post CIK→ticker mapping)
      - `attempted`   — rows we tried to insert (after our local
                        dedup of repeated entries)
      - `err_count`   — ATOM entries the connector dropped because
                        of unrecognised shape, missing CIK, or no
                        ticker mapping (foreign filer / SPV).
                        Surfaced separately so a spike here flags
                        an upstream change OR a stale ticker map
                        before it silently degrades into "ok / 0".

    Empty results are success: an off-hours cron tick can return
    `fetched=0` legitimately during weekends / market holidays.
    """
    rows, err_count = await sec_edgar.get_recent_8k_filings(
        limit=_FETCH_LIMIT,
    )
    fetched = len(rows)
    if not rows:
        return {"fetched": 0, "attempted": 0, "err_count": err_count}

    payload: list[CorporateAnnouncementRow] = []
    seen_hashes: set[str] = set()
    for r in rows:
        h = r.dedup_hash
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        payload.append(CorporateAnnouncementRow(
            market=r.market,
            symbol=r.symbol,
            announced_at=_ensure_utc(r.announced_at),
            category=r.category,
            title=r.title,
            body=r.body,
            source_url=r.source_url,
            source=r.source,
            dedup_hash=h,
        ))

    async with AsyncSessionLocal() as db:
        await insert_corporate_announcements(db, payload)
    return {
        "fetched": fetched,
        "attempted": len(payload),
        "err_count": err_count,
    }


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
