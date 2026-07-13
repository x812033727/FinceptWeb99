"""Daily TAIEX (大盤加權指數) history ingest.

Runs once daily after TWSE closes (07:10 UTC = 15:10 Taipei). One
TWSE call returns the entire current-month FMTQIK series (one row
per trading day). Bars are upserted into `ohlcv_daily` under symbol
`_TAIEX` (underscore prefix marks this as a synthetic index row,
not a real stock code) so:

  - the existing `read_ohlcv_range` helper serves it without
    touching the schema
  - the StockDetailPage K-line code path can render TAIEX charts via
    the same endpoint
  - the discussion subsystem can read 30 days of the index alongside
    its current quote (`get_index` becomes "current + history")

Failure handling and lock semantics mirror `tasks/ingest_margin_tw.py`
— monthly cadence + idempotent UPSERT mean a missed tick re-fills
on the next run without dupe rows; exponential backoff prevents a
TWSE outage from thrashing the upstream every interval.
"""
import logging
from datetime import date

import httpx

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    OhlcvBar,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_ohlcv_bars,
)
from tasks._runner import TaskOutcome, run_ingest_task

log = logging.getLogger(__name__)

JOB_ID = "ingest_taiex_history"
MARKET = "TW"
TAIEX_SYMBOL = "_TAIEX"

_LOCK_KEY = "lock:ingest_taiex_history"
_LOCK_TTL = 3 * 60   # one TWSE call + write


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
    row_count, latest_ts = await _do_run()
    return TaskOutcome(row_count=row_count, latest_data_ts=latest_ts)


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


async def _do_run() -> tuple[int, date | None]:
    today = date.today()
    rows = await twse.get_taiex_history(today)
    if not rows:
        log.info("ingest_taiex_history.empty_response")
        return 0, None

    bars = [
        OhlcvBar.from_connector_row(MARKET, TAIEX_SYMBOL, "twse", r)
        for r in rows
    ]
    bars = [b for b in bars if b is not None]

    async with AsyncSessionLocal() as db:
        written = await upsert_ohlcv_bars(db, bars)

    latest_ts = max((b.ts for b in bars), default=None)
    return written, latest_ts
