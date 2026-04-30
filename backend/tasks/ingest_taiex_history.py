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

Failure handling and lock semantics mirror `tasks/ingest_ohlcv_tw.py`
— monthly cadence + idempotent UPSERT mean a missed tick re-fills
on the next run without dupe rows.
"""
import logging
from datetime import date

import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    OhlcvBar,
    record_health,
    upsert_ohlcv_bars,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_taiex_history"
MARKET = "TW"
TAIEX_SYMBOL = "_TAIEX"

_LOCK_KEY = "lock:ingest_taiex_history"
_LOCK_TTL = 3 * 60   # one TWSE call + write


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_taiex_history.skipped_lock_held")
        return
    try:
        await _do_run()
    except Exception as exc:
        log.exception("ingest_taiex_history.failed")
        await record_health(JOB_ID, ok=False, error=str(exc))
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> None:
    today = date.today()
    rows = await twse.get_taiex_history(today)
    if not rows:
        log.info("ingest_taiex_history.empty_response")
        await record_health(JOB_ID, ok=True, row_count=0)
        return

    bars = [
        OhlcvBar.from_connector_row(MARKET, TAIEX_SYMBOL, "twse", r)
        for r in rows
    ]
    bars = [b for b in bars if b is not None]

    async with AsyncSessionLocal() as db:
        written = await upsert_ohlcv_bars(db, bars)

    log.info("ingest_taiex_history.done", extra={"rows_written": written})
    await record_health(JOB_ID, ok=True, row_count=written)
