"""Daily TW institutional investor flows ingest.

Runs once daily after TWSE closes (06:50 UTC = 14:50 Taipei). One
TWSE call returns ALL ~2000 stocks' institutional buy/sell volumes
for the latest trading day in a single payload — no per-symbol fan-
out, no rate-limit concerns. Rows are upserted into
`tw_institutional_daily` so the read tier (`tw_market_service.
get_institutional`) and the discussion subsystem's
`read_top_foreign_buyers` aggregator can serve from DB without
hitting an upstream every time.

Failure handling mirrors `tasks/ingest_news_tw.py`: HTTP errors are
formatted with a hint, repeated failures arm an exponential backoff
(1h → 6h cap), and the backoff-skip path preserves the most recent
real error in the health row so admins don't have to wait the
cooldown out to see the cause.

Multi-pod safe via Redis SET-NX lock.
"""
import logging
from datetime import date, datetime, timedelta

import httpx

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    InstitutionalDailyRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_institutional_daily,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_institutional_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_institutional_tw"
_LOCK_TTL = 5 * 60   # one TWSE call + write


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
        log.info("ingest_institutional_tw.skipped_lock_held")
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
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_institutional_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_institutional_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    """Pull all-stocks-one-day from TWSE, upsert into DB. Returns row
    count. Empty payload (TWSE returned no rows — typically a holiday
    or a TWSE outage) is treated as success with row_count=0 so the
    cron's healthy steady state is preserved."""
    today = date.today()
    rows = await twse.get_institutional(today)
    if not rows:
        return 0

    payload = [
        InstitutionalDailyRow(
            market=MARKET,
            symbol=r["symbol"],
            ts=today,
            fini_buy=r.get("fini_buy"),
            fini_sell=r.get("fini_sell"),
            sitc_buy=r.get("sitc_buy"),
            sitc_sell=r.get("sitc_sell"),
            dealer_buy=r.get("dealer_buy"),
            dealer_sell=r.get("dealer_sell"),
            source="twse",
        )
        for r in rows
        if r.get("symbol")
    ]
    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_institutional_daily(db, payload)
