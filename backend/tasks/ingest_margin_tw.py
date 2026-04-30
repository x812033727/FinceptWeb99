"""Daily TW margin / short balance ingest.

Runs once daily after TWSE closes (07:00 UTC = 15:00 Taipei). One
TWSE call (`/fund/MI_MARGN`) returns ALL stocks' margin purchase /
balance + short sale / balance for the latest trading day. Rows are
upserted into `tw_margin_daily` so the read tier and the discussion
subsystem's `read_market_margin_balance_trend` aggregator serve
from DB.

Mirrors `tasks/ingest_institutional_tw.py` for failure handling,
backoff, and lock semantics — see that module's docstring for the
full pattern.
"""
import logging
from datetime import date

import httpx

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    MarginDailyRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_margin_daily,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_margin_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_margin_tw"
_LOCK_TTL = 5 * 60


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
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_margin_tw.skipped_lock_held")
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
                "ingest_margin_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_margin_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    today = date.today()
    rows = await twse.get_margin(today)
    if not rows:
        return 0

    payload = [
        MarginDailyRow(
            market=MARKET,
            symbol=r["symbol"],
            ts=today,
            margin_purchase=r.get("margin_purchase"),
            margin_balance=r.get("margin_balance"),
            short_sale=r.get("short_sale"),
            short_balance=r.get("short_balance"),
            source="twse",
        )
        for r in rows
        if r.get("symbol")
    ]
    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_margin_daily(db, payload)
