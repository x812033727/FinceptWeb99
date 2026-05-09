"""Daily TW fundamentals ingest task.

One TWSE call (`get_all_valuation_ratios` / BWIBBU_ALL) returns the full
day's PE / PB / dividend_yield for every TWSE-listed security. We
upsert one row per (symbol, today) into `fundamentals_snapshots` so the
read path can serve recent ratios from DB during a TWSE outage and
operators can chart historical valuations without TWSE retro-queries.

Cadence: daily 06:45 UTC (after the 06:30 OHLCV ingest). Multi-pod
safe via Redis SET-NX lock.

Failure handling and lock semantics mirror `tasks/ingest_margin_tw.py` —
exponential 1h → 6h backoff, last-error preserved across the cooldown
window so admins see why the job was skipped without scraping logs.
"""
import logging
from datetime import date

import httpx

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    FundamentalsSnapshotRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_fundamentals_snapshots,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_fundamentals_tw"

_LOCK_KEY = "lock:ingest_fundamentals_tw"
_LOCK_TTL = 10 * 60   # 10 min — one bulk call typically completes in seconds


_HTTP_HINTS: dict[int, str] = {
    400: "TWSE rejected the request — query may be malformed",
    403: "TWSE refused — UA blocked or geo-restricted",
    429: "TWSE rate-limit — backoff and retry later",
    500: "TWSE upstream error",
    502: "TWSE bad gateway",
    503: "TWSE unavailable",
    504: "TWSE gateway timeout",
}


def _format_error(exc: BaseException) -> str:
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
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_fundamentals_tw.skipped_lock_held")
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
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining{tail})"
                ),
            )
            return

        try:
            row_count, latest_ts = await _do_run()
        except Exception as exc:
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_fundamentals_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_fundamentals_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(
            JOB_ID, ok=True, row_count=row_count,
            latest_data_ts=latest_ts,
        )
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> tuple[int, date | None]:
    """Fetch valuation ratios + upsert. Empty TWSE response returns
    (0, None) — recorded as ok=True row_count=0 (an empty BWIBBU_ALL
    is rare but legitimate, e.g. early-morning pre-publish window)."""
    ratios = await twse.get_all_valuation_ratios()
    if not ratios:
        log.warning("ingest_fundamentals_tw.empty_result")
        return 0, None

    today = date.today()
    rows = [
        FundamentalsSnapshotRow(
            market="TW",
            symbol=symbol,
            as_of=today,
            pe_ratio=v.get("pe_ratio"),
            pb_ratio=v.get("pb_ratio"),
            dividend_yield=v.get("dividend_yield"),
            eps=None,
            revenue=None,
            payload=None,
            source="twse",
        )
        for symbol, v in ratios.items()
        if symbol  # paranoia guard against empty keys
    ]

    async with AsyncSessionLocal() as db:
        written = await upsert_fundamentals_snapshots(db, rows)

    return written, today
