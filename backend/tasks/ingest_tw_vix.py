"""Daily TAIWAN VIX (臺指選擇權波動率指數) ingest (PR #283).

TAIFEX publishes the daily close of the 臺指選擇權波動率指數 on
their public statistics download page. We pull a 30-day window
every tick — one HTTP request, ~30 rows of CSV — and upsert into
``tw_vix_daily``. Backfill is automatic: re-running the cron
fills any missed days within the window (TAIFEX corrections to
prior closes also propagate via ON CONFLICT DO UPDATE).

Schedule: 16:00 Taipei (08:00 UTC), 1.5 hours after the cash market
close (13:30) but before the FinMind sponsor cluster (10:00+ UTC),
so a TAIFEX outage doesn't cascade into FinMind quota burn.

Look-ahead protection: VIX closes are insert-only — no need for
backtest masking on the read tier.
"""
import logging
from datetime import date, timedelta

import httpx

import data.tw.taifex_connector as taifex
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    VixDailyRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_tw_vix_daily,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_tw_vix"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_tw_vix"
_LOCK_TTL = 5 * 60
_LOOKBACK_DAYS = 30


_HTTP_HINTS: dict[int, str] = {
    400: "TAIFEX rejected the request — query may be malformed",
    403: "TAIFEX refused — UA blocked or geo-restricted",
    429: "TAIFEX rate-limit — backoff and retry later",
    500: "TAIFEX upstream error",
    502: "TAIFEX bad gateway",
    503: "TAIFEX unavailable",
    504: "TAIFEX gateway timeout",
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
        log.info("ingest_tw_vix.skipped_lock_held")
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
            row_count = await _do_run()
        except Exception as exc:
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_tw_vix.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_tw_vix.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    end = date.today()
    start = end - timedelta(days=_LOOKBACK_DAYS)
    items = await taifex.get_vix_history(start, end)
    if not items:
        return 0

    rows: list[VixDailyRow] = []
    for item in items:
        try:
            ts = date.fromisoformat(str(item["date"]))
        except (KeyError, ValueError, TypeError):
            continue
        try:
            value = float(item["value"])
        except (KeyError, ValueError, TypeError):
            continue
        rows.append(VixDailyRow(
            market=MARKET, ts=ts, vix_value=value, source="taifex",
        ))
    if not rows:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_tw_vix_daily(db, rows)
