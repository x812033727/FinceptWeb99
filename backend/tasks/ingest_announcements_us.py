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
from tasks._runner import TaskOutcome, run_ingest_task

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


async def _body() -> TaskOutcome:
    counters = await _do_run()
    status = (
        f"fetched={counters['fetched']} attempted={counters['attempted']}"
    )
    if counters["err_count"]:
        status += f" parse_errors={counters['err_count']}"
    return TaskOutcome(
        row_count=counters["attempted"], status=status, done_extra=counters,
    )


async def run() -> None:
    """APScheduler entry point."""
    await run_ingest_task(
        job_id=JOB_ID, lock_key=_LOCK_KEY, lock_ttl=_LOCK_TTL, log=log,
        acquire_lock=acquire_lock, release_lock=release_lock,
        backoff_remaining_seconds=backoff_remaining_seconds,
        get_failure_count=get_failure_count, get_health=get_health,
        record_health=record_health, record_failure=record_failure,
        clear_failures=clear_failures,
        body=_body, format_error=_format_sec_error, log_backoff_skip=True,
    )


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
