"""Daily TW margin / short balance ingest.

Runs once daily after TWSE closes (07:00 UTC = 15:00 Taipei). One
TWSE call (`/fund/MI_MARGN`) returns ALL stocks' margin purchase /
balance + short sale / balance for the latest trading day. Rows are
upserted into `tw_margin_daily` so the read tier and the discussion
subsystem's `read_market_margin_balance_trend` aggregator serve
from DB.

Mirrors `tasks/ingest_institutional_tw.py` for failure handling,
backoff, lock semantics, and — since this rewrite — the pending-day
walk itself: asking only for `date.today()` meant a missed day (e.g.
Friday's data published after that day's runs) could never self-heal,
and the gap classifier's witness path was dead for margin because no
past day was ever in play. See that module's docstring for the full
pattern this one now shares.
"""
import asyncio
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import select

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from models.tw_chip_metrics import TwMarginDaily
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
from tasks._market_wide import pending_market_days
from tasks._runner import TaskOutcome, run_ingest_task
from tasks.chip_outcome import classify_chip_outcome

log = logging.getLogger(__name__)

JOB_ID = "ingest_margin_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_margin_tw"
# Up to `_LOOKBACK_DAYS` weekday fetches at `_PACING_SECONDS` apart, plus
# the writes — same reasoning as institutional's TTL: generous enough
# that a cold-start backfill can't have the lock expire underneath it.
_LOCK_TTL = 30 * 60

# Margin history need is shallow compared to institutional flows — a
# short lookback still covers a multi-day outage without a cold table
# trying to reconstruct a long backfill in one tick.
_LOOKBACK_DAYS = 10
# TWSE serves the legacy table endpoint without documented rate limits,
# but hammering sequential requests is impolite and invites a block.
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
    total, ok, status = await _do_run()
    return TaskOutcome(row_count=total, ok=ok, status=status)


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
            select(TwMarginDaily.ts)
            .where(
                TwMarginDaily.market == MARKET,
                TwMarginDaily.ts >= cutoff,
            )
            .distinct()
        )
        return {r if isinstance(r, date) else r.date() for r in rows.all()}


async def _ingest_day(day: date) -> int:
    """Fetch and write one session. Returns rows written (0 on a
    non-trading day, which TWSE answers with an empty table)."""
    rows = await twse.get_margin(day)
    payload = [
        MarginDailyRow(
            market=MARKET,
            symbol=r["symbol"],
            ts=day,
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


async def _do_run() -> tuple[int, bool, str | None]:
    """Walk every pending weekday in the window, writing each as it
    lands. Returns (total rows written, ok, status).

    Weekends and holidays come back empty and are simply skipped —
    they are never archived, so they stay in `pending_market_days`
    forever, which is the price of not maintaining a holiday calendar
    here. The cost is a handful of empty requests per run. A day that
    comes back empty but has TW price bars in `ohlcv_daily` (our own
    archive proves the market traded) is a real gap, not a holiday —
    see `tasks.chip_outcome`.
    """
    # Single snapshot, not a fresh `date.today()` per use — see
    # `ingest_institutional_tw._do_run` for why: the walk below sleeps
    # `_PACING_SECONDS` between days, so a run straddling midnight could
    # otherwise see "today" advance between the walk and the
    # classify_chip_outcome() call below it.
    today = date.today()
    days = pending_market_days(
        today, _LOOKBACK_DAYS, await _archived_days(),
    )
    day_rows: dict[date, int] = {}
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
                "ingest_margin_tw.day_failed",
                extra={"day": day.isoformat(), "error": str(exc)},
            )
            continue
        succeeded += 1
        total += written
        day_rows[day] = written

    if failures and succeeded == 0:
        # Every day failed — this is an outage, not a quiet holiday.
        # Re-raise so the runner records it and arms the backoff
        # instead of reporting a healthy zero-row run.
        raise failures[0][1]
    if failures:
        log.warning(
            "ingest_margin_tw.partial",
            extra={
                "days_requested": len(days),
                "days_failed": len(failures),
                "rows": total,
            },
        )

    past_empty = [d for d, r in day_rows.items() if r == 0]
    traded: set[date] = set()
    if past_empty:
        async with AsyncSessionLocal() as db:
            hits = (await db.scalars(
                select(OhlcvDaily.ts).where(
                    OhlcvDaily.market == "TW",
                    OhlcvDaily.ts.in_(past_empty),
                    OhlcvDaily.symbol == "2330",   # one liquid witness row is enough
                )
            )).all()
        traded = {t.date() if hasattr(t, "date") else t for t in hits}

    ok, status = classify_chip_outcome(
        day_rows=day_rows, today=today, traded=traded,
    )
    return total, ok, status
