"""Half-hourly TW MOPS material-info ingest (PR-D1).

Sister task to `ingest_news_tw`. Pulls the daily 重大訊息 list from
MOPS via `data.tw.mops_announcements_connector.get_recent_announcements`
and dedups via sha256(symbol + normalized title + link). Articles
that survive the dedup land in `corporate_announcements` and the
existing hourly `score_news_sentiment` cron will pick them up on
its next pass — same LLM scorer, same daily cap, same fail-closed
behaviour.

Why 30-min cadence (vs the news cron's hourly):

  - Material-info disclosures matter most while the market is still
    open. A 14:32 disclosure that lands in the discussion ctx at
    15:00 (next cron tick) is far more actionable than landing at
    16:00 — by which time the price has usually already moved.
  - MOPS rate-limits aren't documented but the ajax endpoint comfortably
    serves 1 call / 30 min without complaint; a single call returns
    the full day's listing so we don't fan out per symbol.

Failure handling mirrors `ingest_news_tw` exactly — exponential
backoff on repeated failure, last-error preservation in the health
row so the IngestHealthCard reflects WHY the cron is in cooldown
without operators having to clear Redis.

Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import UTC, datetime

import httpx

import data.tw.mops_announcements_connector as mops_announcements
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

JOB_ID = "ingest_announcements_tw"

_LOCK_KEY = "lock:ingest_announcements_tw"
_LOCK_TTL = 5 * 60   # 5 min — bulk call typically completes in 1-3 s
_FETCH_LIMIT = 200   # MOPS listing is ~50-150 disclosures on a normal day


_HTTP_HINTS: dict[int, str] = {
    400: "MOPS rejected the request — endpoint may have moved",
    403: "MOPS refused — UA blocked or session cookie required",
    429: "MOPS rate-limit — backoff and retry later",
    500: "MOPS upstream error",
    502: "MOPS bad gateway",
    503: "MOPS unavailable",
    504: "MOPS gateway timeout",
}


def _format_announce_error(exc: BaseException) -> str:
    """Operator-friendly one-line error summary. Same shape as
    `ingest_news_tw._format_news_error` so the IngestHealthCard
    columns stay consistent."""
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
        log.info("ingest_announcements_tw.skipped_lock_held")
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
                "ingest_announcements_tw.skipped_backoff",
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
            detail = _format_announce_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_announcements_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info("ingest_announcements_tw.done", extra=counters)
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
    """Pull MOPS, parse, dedup, persist.

    Counters returned for the health record:
      - `fetched`     — rows the connector returned (post-parse)
      - `attempted`   — rows we tried to insert (after our local dedup)
      - `err_count`   — MOPS rows the connector dropped because of
                        unrecognised shape (column rotation, missing
                        required field). Surfaced separately so a
                        spike here flags an upstream change before
                        it silently degrades into "ok / 0".

    Empty results are success: `fetched=0` is normal on weekends and
    public holidays when MOPS still serves the endpoint but the day's
    listing is empty.
    """
    rows, err_count = await mops_announcements.get_recent_announcements(
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
            # MOPS occasionally repeats rows in the same response when
            # an issuer files an amendment to an existing disclosure.
            # The DB-level on-conflict handles cross-run dedup; this
            # in-memory check just keeps the per-call payload clean.
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
    """Belt-and-suspenders: connector returns UTC datetimes, but
    serialize back to UTC defensively so an upstream change doesn't
    silently land naive datetimes in `announced_at`."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
