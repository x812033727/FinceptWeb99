"""Daily TW OHLCV ingest task.

Runs once daily after TWSE closes (06:30 UTC). For every symbol in
`_exchange_map`, fetch the current month's bars from TWSE; on TWSE
failure, fall back to FinMind for the last 7 days. Bars are upserted
into `ohlcv_daily` so the read path can serve K-lines from DB even
when both upstreams are simultaneously unavailable.

The task is multi-pod safe via a Redis SET-NX lock — the holder
renews-by-TTL so a crashed pod doesn't wedge tomorrow's run.

Failure handling mirrors `tasks/ingest_margin_tw.py` — exponential
1h → 6h backoff, last-error preserved across the cooldown window so
admins see why the job was skipped without scraping logs.
"""
import asyncio
import logging
from datetime import date, timedelta

import httpx

import data.tw.finmind_connector as finmind
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

JOB_ID = "ingest_ohlcv_tw"

_LOCK_KEY = "lock:ingest_ohlcv_tw"
_LOCK_TTL = 30 * 60   # 30 min covers 1.1 s/req × ~2000 syms = ~37 min worst case

# TWSE rate limit is one request at a time with a ~1 s pacing delay
# already enforced inside `twse._wait_for_token`. We don't gather()
# concurrently — TWSE 429s the moment two requests overlap. The
# inter-symbol sleep below is belt-and-braces.
_TWSE_INTER_SYMBOL_SLEEP = 0.0


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


async def _do_run() -> tuple[int, date | None]:
    """Returns (rows_written, latest_bar_ts). Raises on universe load
    failure or when every symbol's both upstreams failed (treat as
    real outage instead of a fake-success row_count=0). Per-symbol
    legitimate-empty (weekend, halt) does NOT count as a failure."""
    symbols = await _load_symbols()
    if not symbols:
        log.warning("ingest_ohlcv_tw.no_symbols")
        # No upstream call yet — treat as a benign "universe not loaded";
        # `run()` will record ok=True with row_count=0 (clear_failures
        # path) since this isn't an outage.
        return 0, None

    today = date.today()
    total = 0
    latest_ts: date | None = None
    dual_failures = 0   # both TWSE + FinMind raised for this symbol

    async with AsyncSessionLocal() as db:
        for sym in symbols:
            bars, both_failed = await _fetch_one(sym, today)
            if both_failed:
                dual_failures += 1
                continue
            if not bars:
                continue

            written = await upsert_ohlcv_bars(db, bars)
            total += written
            for b in bars:
                if latest_ts is None or b.ts > latest_ts:
                    latest_ts = b.ts

            if _TWSE_INTER_SYMBOL_SLEEP:
                await asyncio.sleep(_TWSE_INTER_SYMBOL_SLEEP)

    if total == 0 and dual_failures == len(symbols):
        # Every symbol's BOTH upstreams failed — real outage, not a
        # quiet day. Promote to a top-level error so the backoff path
        # arms; otherwise a TWSE+FinMind dual-down would record
        # ok=True row_count=0 and look identical to a holiday.
        raise RuntimeError(
            f"all {dual_failures} symbols failed both upstreams"
        )
    return total, latest_ts


async def _load_symbols() -> list[str]:
    """Refresh the TW exchange map first, then snapshot it.

    The scheduler's daily `tw_symbol_map` job also keeps `_exchange_map`
    fresh, but we re-refresh inline so a fresh deployment doesn't miss
    bars on day 1 (APScheduler's IntervalTrigger only fires after one
    full interval has elapsed).
    """
    from services.tw_market_service import _exchange_map, refresh_symbol_map

    if not _exchange_map:
        try:
            await refresh_symbol_map()
        except Exception as exc:
            log.warning("ingest_ohlcv_tw.symbol_map_refresh_failed",
                        extra={"error": str(exc)})

    return sorted(_exchange_map.keys())


async def _fetch_one(symbol: str, today: date) -> tuple[list[OhlcvBar], bool]:
    """TWSE first (full month for `today`), FinMind fallback (last 7 days).

    Returns ``(bars, both_failed)``. ``both_failed`` is True iff TWSE
    AND FinMind both raised — caller uses this to detect a real
    upstream outage. A successful call that returned no rows
    (legitimate weekend / halt) reports ``([], False)``.

    TWSE returns the entire month containing `today` in one call so we
    pick up any prior days we might have missed during a partial run.
    """
    twse_failed = False
    try:
        rows = await twse.get_daily_ohlcv(symbol, today)
        bars = [
            OhlcvBar.from_connector_row("TW", symbol, "twse", r) for r in rows
        ]
        bars = [b for b in bars if b is not None]
        if bars:
            return bars, False
    except Exception as exc:
        twse_failed = True
        log.warning("ingest_ohlcv_tw.twse_failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        start = (today - timedelta(days=7)).isoformat()
        rows = await finmind.get_daily_ohlcv(symbol, start)
        bars = [
            OhlcvBar.from_connector_row("TW", symbol, "finmind", r) for r in rows
        ]
        return [b for b in bars if b is not None], False
    except Exception as exc:
        log.warning("ingest_ohlcv_tw.finmind_failed",
                    extra={"symbol": symbol, "error": str(exc)})
        return [], twse_failed
