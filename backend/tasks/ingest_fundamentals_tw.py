"""Daily TW fundamentals ingest task.

One TWSE call (`get_all_valuation_ratios` / BWIBBU_ALL) returns the full
day's PE / PB / dividend_yield for every TWSE-listed security. We
upsert one row per (symbol, today) into `fundamentals_snapshots` so the
read path can serve recent ratios from DB during a TWSE outage and
operators can chart historical valuations without TWSE retro-queries.

Cadence: daily 06:45 UTC (after the 06:30 OHLCV ingest). Multi-pod
safe via Redis SET-NX lock.
"""
import logging
from datetime import date

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    FundamentalsSnapshotRow,
    record_health,
    upsert_fundamentals_snapshots,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_fundamentals_tw"

_LOCK_KEY = "lock:ingest_fundamentals_tw"
_LOCK_TTL = 10 * 60   # 10 min — one bulk call typically completes in seconds


async def run() -> None:
    """Entry point invoked by APScheduler."""
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_fundamentals_tw.skipped_lock_held")
        return
    try:
        await _do_run()
    except Exception as exc:
        log.exception("ingest_fundamentals_tw.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> None:
    try:
        ratios = await twse.get_all_valuation_ratios()
    except Exception as exc:
        log.warning("ingest_fundamentals_tw.twse_failed", extra={"error": str(exc)})
        await record_health(JOB_ID, ok=False, error=f"twse_unavailable: {exc}")
        return

    if not ratios:
        log.warning("ingest_fundamentals_tw.empty_result")
        await record_health(JOB_ID, ok=False, error="empty_result")
        return

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

    log.info("ingest_fundamentals_tw.done", extra={"rows_written": written})
    await record_health(JOB_ID, ok=True, row_count=written)
