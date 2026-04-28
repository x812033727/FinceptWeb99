"""Daily TW OHLCV ingest task.

Runs once daily after TWSE closes (06:30 UTC). For every symbol in
`_exchange_map`, fetch the current month's bars from TWSE; on TWSE
failure, fall back to FinMind for the last 7 days. Bars are upserted
into `ohlcv_daily` so the read path can serve K-lines from DB even
when both upstreams are simultaneously unavailable.

The task is multi-pod safe via a Redis SET-NX lock — the holder
renews-by-TTL so a crashed pod doesn't wedge tomorrow's run.
"""
import asyncio
import logging
from datetime import date, timedelta

import data.tw.finmind_connector as finmind
import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    OhlcvBar,
    record_health,
    upsert_ohlcv_bars,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_ohlcv_tw"

_LOCK_KEY = "lock:ingest_ohlcv_tw"
_LOCK_TTL = 30 * 60   # 30 min covers 1.1 s/req × ~2000 syms = ~37 min worst case

# TWSE rate limit is one request at a time with a ~1 s pacing delay
# already enforced inside `twse._wait_for_token`. We don't gather()
# concurrently — TWSE 429s the moment two requests overlap. The
# inter-symbol sleep below is belt-and-braces.
_TWSE_INTER_SYMBOL_SLEEP = 0.0


async def run() -> None:
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_ohlcv_tw.skipped_lock_held")
        return
    try:
        await _do_run()
    except Exception as exc:
        log.exception("ingest_ohlcv_tw.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> None:
    symbols = await _load_symbols()
    if not symbols:
        log.warning("ingest_ohlcv_tw.no_symbols")
        await record_health(JOB_ID, ok=False, error="no_symbols")
        return

    today = date.today()
    total = 0
    failures = 0

    async with AsyncSessionLocal() as db:
        for sym in symbols:
            try:
                bars = await _fetch_one(sym, today)
            except Exception as exc:
                failures += 1
                log.warning("ingest_ohlcv_tw.symbol_failed",
                            extra={"symbol": sym, "error": str(exc)})
                continue

            if not bars:
                continue

            written = await upsert_ohlcv_bars(db, bars)
            total += written

            if _TWSE_INTER_SYMBOL_SLEEP:
                await asyncio.sleep(_TWSE_INTER_SYMBOL_SLEEP)

    log.info(
        "ingest_ohlcv_tw.done",
        extra={"symbols": len(symbols), "rows_written": total, "failures": failures},
    )
    await record_health(JOB_ID, ok=True, row_count=total)


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


async def _fetch_one(symbol: str, today: date) -> list[OhlcvBar]:
    """TWSE first (full month for `today`), FinMind fallback (last 7 days).

    TWSE returns the entire month containing `today` in one call so we
    pick up any prior days we might have missed during a partial run.
    """
    try:
        rows = await twse.get_daily_ohlcv(symbol, today)
        bars = [
            OhlcvBar.from_connector_row("TW", symbol, "twse", r) for r in rows
        ]
        bars = [b for b in bars if b is not None]
        if bars:
            return bars
    except Exception as exc:
        log.warning("ingest_ohlcv_tw.twse_failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        start = (today - timedelta(days=7)).isoformat()
        rows = await finmind.get_daily_ohlcv(symbol, start)
        bars = [
            OhlcvBar.from_connector_row("TW", symbol, "finmind", r) for r in rows
        ]
        return [b for b in bars if b is not None]
    except Exception as exc:
        log.warning("ingest_ohlcv_tw.finmind_failed",
                    extra={"symbol": symbol, "error": str(exc)})
        return []
