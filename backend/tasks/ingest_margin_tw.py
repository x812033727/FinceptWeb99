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
from tasks._runner import TaskOutcome, run_ingest_task
from tasks.chip_outcome import classify_chip_outcome

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


async def _body() -> TaskOutcome:
    total, ok, status = await _do_run()
    return TaskOutcome(row_count=total, ok=ok, status=status)


async def run() -> None:
    await run_ingest_task(
        job_id=JOB_ID, lock_key=_LOCK_KEY, lock_ttl=_LOCK_TTL, log=log,
        acquire_lock=acquire_lock, release_lock=release_lock,
        backoff_remaining_seconds=backoff_remaining_seconds,
        get_failure_count=get_failure_count, get_health=get_health,
        record_health=record_health, record_failure=record_failure,
        clear_failures=clear_failures,
        body=_body, format_error=_format_error,
    )


async def _do_run() -> tuple[int, bool, str | None]:
    today = date.today()
    rows = await twse.get_margin(today)

    written = 0
    if rows:
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
        if payload:
            async with AsyncSessionLocal() as db:
                written = await upsert_margin_daily(db, payload)

    # Unlike the institutional walk, this job only ever asks for `today` —
    # there is no past day in play, so there is no gap to witness against
    # ohlcv_daily (that check only ever fires for `d < today`). Weekends
    # and holidays answer empty exactly like a weekday before the evening
    # publication; omit them from day_rows so they land in `idle` rather
    # than misreporting `not_yet_published`, mirroring how the
    # institutional walk's `pending_market_days` filters weekends out
    # before they ever reach the classifier.
    day_rows: dict[date, int] = {}
    if today.weekday() < 5 or written:
        day_rows[today] = written

    ok, status = classify_chip_outcome(
        day_rows=day_rows, today=today, traded=set(),
    )
    return written, ok, status
