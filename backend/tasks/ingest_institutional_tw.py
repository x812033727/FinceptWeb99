"""Daily TW institutional investor flows ingest.

Runs once daily after TWSE closes (06:50 UTC = 14:50 Taipei). One
TWSE call returns ALL ~2000 stocks' institutional buy/sell volumes
for a given trading day in a single payload — no per-symbol fan-out,
no rate-limit concerns. Rows are upserted into
`tw_institutional_daily` so the read tier (`tw_market_service.
get_institutional`) and the discussion subsystem's
`read_top_foreign_buyers` aggregator can serve from DB without
hitting an upstream every time.

The job walks a window of pending trading days rather than asking for
`today` alone. Asking only for today means a single transient failure
loses that session permanently: the next run asks for the next day and
nothing ever revisits the hole. That is what happened on 2026-07-20 and
07-21 — two `timeout` runs left the table frozen at 07-17 while the
daily strategies kept screening on three-day-old 籌碼 and the panel kept
being handed it as current. `pending_market_days` (see
`tasks/_market_wide`) makes the steady state one call for today and
turns an outage into something that heals itself on the next tick.

Each day is fetched and written on its own so a failure mid-window
keeps what already landed. A run only reports failure when *no* day
succeeded — a partially-filled window is genuine progress and the
remaining gaps are picked up next tick — but it never reports success
while silently dropping every day.

Failure handling mirrors `tasks/ingest_news_tw.py`: HTTP errors are
formatted with a hint, repeated failures arm an exponential backoff
(1h → 6h cap), and the backoff-skip path preserves the most recent
real error in the health row so admins don't have to wait the
cooldown out to see the cause.

Multi-pod safe via Redis SET-NX lock.
"""
import asyncio
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import select

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.tw_chip_metrics import TwInstitutionalDaily
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
from tasks._market_wide import pending_market_days
from tasks._runner import TaskOutcome, run_ingest_task

log = logging.getLogger(__name__)

JOB_ID = "ingest_institutional_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_institutional_tw"
# Up to `_LOOKBACK_DAYS` weekday fetches at `_PACING_SECONDS` apart,
# plus the writes. Generous so a cold-start backfill can't have the
# lock expire underneath it and let a second pod start duplicating work.
_LOCK_TTL = 30 * 60

# How far back a run is willing to heal. Long enough to cover a holiday
# stretch plus a multi-day outage, short enough that a cold table
# doesn't try to reconstruct a year in one tick.
_LOOKBACK_DAYS = 30
# TWSE serves the legacy table endpoint without documented rate limits,
# but hammering ~20 sequential requests is impolite and invites a block.
_PACING_SECONDS = 1.2


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
    return TaskOutcome(row_count=await _do_run())


async def run() -> None:
    """Entry point invoked by APScheduler."""
    await run_ingest_task(
        job_id=JOB_ID, lock_key=_LOCK_KEY, lock_ttl=_LOCK_TTL, log=log,
        acquire_lock=acquire_lock, release_lock=release_lock,
        backoff_remaining_seconds=backoff_remaining_seconds,
        get_failure_count=get_failure_count, get_health=get_health,
        record_health=record_health, record_failure=record_failure,
        clear_failures=clear_failures,
        body=_body, format_error=_format_error,
    )


async def _archived_days() -> set[date]:
    """Trading days already in the archive within the lookback window."""
    cutoff = date.today() - timedelta(days=_LOOKBACK_DAYS)
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(TwInstitutionalDaily.ts)
            .where(
                TwInstitutionalDaily.market == MARKET,
                TwInstitutionalDaily.ts >= cutoff,
            )
            .distinct()
        )
        return {r if isinstance(r, date) else r.date() for r in rows.all()}


async def _ingest_day(day: date) -> int:
    """Fetch and write one session. Returns rows written (0 on a
    non-trading day, which TWSE answers with an empty table)."""
    rows = await twse.get_institutional(day)
    payload = [
        InstitutionalDailyRow(
            market=MARKET,
            symbol=r["symbol"],
            ts=day,
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


async def _do_run() -> int:
    """Walk every pending weekday in the window, writing each as it
    lands. Returns total rows written.

    Weekends and holidays come back empty and are simply skipped —
    they are never archived, so they stay in `pending_market_days`
    forever, which is the price of not maintaining a holiday calendar
    here. The cost is a handful of empty requests per run.
    """
    days = pending_market_days(
        date.today(), _LOOKBACK_DAYS, await _archived_days(),
    )
    if not days:
        return 0

    total = 0
    succeeded = 0
    failures: list[tuple[date, BaseException]] = []
    for index, day in enumerate(days):
        if index:
            await asyncio.sleep(_PACING_SECONDS)
        try:
            written = await _ingest_day(day)
        except Exception as exc:  # noqa: BLE001 — re-raised below if total
            failures.append((day, exc))
            log.warning(
                "ingest_institutional_tw.day_failed",
                extra={"day": day.isoformat(), "error": str(exc)},
            )
            continue
        succeeded += 1
        total += written

    if failures and succeeded == 0:
        # Every day failed — this is an outage, not a quiet holiday.
        # Re-raise so the runner records it and arms the backoff
        # instead of reporting a healthy zero-row run.
        raise failures[0][1]
    if failures:
        log.warning(
            "ingest_institutional_tw.partial",
            extra={
                "days_requested": len(days),
                "days_failed": len(failures),
                "rows": total,
            },
        )
    return total
