"""Slow per-symbol TW monthly-revenue ingest via MOPS.

Replaces the FinMind market-wide cron (`ingest_revenue_tw`) which
got paywalled in 2026-04 — see PR #183 for the fail-soft handler
that keeps the FinMind path harmless.

Approach
========

Hourly tick. Each tick processes the next slice of N symbols from the
in-memory `_exchange_map` universe (~1700 listed companies), paced
~5 s apart so a tick fits well inside the hour. A Redis cursor
(`ingest:revenue_tw_slow:cursor`) tracks position across ticks so a
worker restart (deploy) just resumes — no work is lost or repeated
within a cycle. When the cursor wraps past the universe size we bump
a `ingest:revenue_tw_slow:cycle` counter for diag visibility.

At ~75 symbols / hour the universe is fully refreshed every ~22 h.
Monthly revenue only updates once per month (by the 10th, per TW
securities law) so the actual data freshness stays well within the
publish cadence.

The MOPS endpoint we hit (`mops.interinfo.com.tw:8443/mops/api/`)
returns the trailing 12-24 months in one call per symbol, so 75
calls/h covers the whole market without needing per-(year, month)
fan-out.

Per-symbol failures are logged + counted into the tick-summary health
record. Only a complete tick failure (universe empty, MOPS
permanently down) records job-level failure / arms backoff.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

import data.tw.mops_connector as mops
from cache.redis_cache import (
    acquire_lock,
    cache_get,
    cache_incr,
    cache_set,
    release_lock,
)
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    RevenueMonthlyRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_revenue_monthly,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_revenue_tw_slow"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_revenue_tw_slow"
_LOCK_TTL = 30 * 60   # generous — actual work fits in ~6 min
_CURSOR_KEY = "ingest:revenue_tw_slow:cursor"
_CYCLE_KEY = "ingest:revenue_tw_slow:cycle"
_CURSOR_TTL = 7 * 24 * 3600

# Pacing: 75 symbols / hour ≈ 1 every 48 s. We use 5 s gaps inside
# the tick so a 75-symbol batch finishes in ~6 min, leaving plenty
# of headroom in the hourly window. The pacing is partly defensive
# against MOPS bot detection (the legacy `ajax_t05st10_ifrs` endpoint
# went silent for any caller; the new SPA backend hasn't started
# blocking us yet, but a slow drip stays under whatever rate-limit
# they eventually impose).
_BATCH_SIZE = 75
_INTER_SYMBOL_DELAY_SECONDS = 5.0


async def _list_symbols() -> list[str]:
    """Snapshot the in-memory TW symbol universe maintained by
    `services.tw_market_service.refresh_symbol_map`. Returns a sorted
    list so the cursor advances deterministically across restarts."""
    from services.tw_market_service import _exchange_map
    return sorted(_exchange_map.keys())


async def _read_cursor(universe_size: int) -> int:
    """Read the next-symbol index from Redis. Wraps around past the
    universe size; on Redis outage just starts at 0 (a cycle restart)."""
    try:
        raw = await cache_get(_CURSOR_KEY)
    except Exception:
        return 0
    if raw is None:
        return 0
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return 0
    if idx < 0 or idx >= universe_size:
        return 0
    return idx


async def _write_cursor(idx: int) -> None:
    try:
        await cache_set(_CURSOR_KEY, str(idx), _CURSOR_TTL)
    except Exception as exc:
        # Cursor loss isn't fatal — next tick just restarts the cycle.
        log.warning("ingest_revenue_tw_slow.cursor_write_failed",
                    extra={"error": str(exc)})


async def _process_symbol(symbol: str) -> int:
    """Pull recent monthly-revenue rows for one company and upsert.
    Returns the count of rows written. Per-symbol failures are
    swallowed inside `mops.get_monthly_revenue_recent` (returns []),
    so caller doesn't need its own try/except."""
    rows = await mops.get_monthly_revenue_recent(symbol)
    if not rows:
        return 0

    payload: list[RevenueMonthlyRow] = []
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            ts = date.fromisoformat(str(r.get("date", ""))[:10])
        except ValueError:
            continue
        rev = r.get("revenue")
        yoy = r.get("revenue_yoy")
        mom = r.get("revenue_mom")
        # `_to_int` / `_to_float` in mops_connector return None on
        # unparseable input — preserve as NULL rather than coerce
        # to 0 (which would poison `top_revenue_growers` ranking).
        payload.append(RevenueMonthlyRow(
            market=MARKET,
            symbol=sym,
            ts=ts,
            revenue=int(rev) if isinstance(rev, int) else None,
            revenue_yoy=float(yoy) if isinstance(yoy, (int, float)) else None,
            revenue_mom=float(mom) if isinstance(mom, (int, float)) else None,
            source="mops",
        ))

    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_revenue_monthly(db, payload)


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_revenue_tw_slow.skipped_lock_held")
        return
    try:
        # Job-level backoff (same exponential rule the rest of the
        # ingest tasks use). Only triggered if the WHOLE tick fails
        # — per-symbol HTTP errors don't count, since the connector
        # already swallows them.
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
            summary = await _do_run()
        except Exception as exc:
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_revenue_tw_slow.failed",
                extra={"error": str(exc), "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"unexpected: {exc} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info("ingest_revenue_tw_slow.tick_done", extra=summary)
        # `ok=True` even if individual symbols failed — partial success
        # is the expected steady state when one or two symbols don't
        # publish or the connector hits a transient row.
        await record_health(
            JOB_ID, ok=True,
            row_count=summary["rows_written"],
            error=summary["status_text"],
        )
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> dict:
    """Process one batch of `_BATCH_SIZE` symbols starting at the
    Redis cursor. Returns a summary dict for the health record."""
    symbols = await _list_symbols()
    if not symbols:
        # Universe not loaded yet — `tw_symbol_map` cron hasn't run
        # since boot. Cron will retry next hour; this is normal on a
        # fresh deploy.
        return {
            "symbols_in_universe": 0,
            "symbols_processed": 0,
            "symbols_with_data": 0,
            "rows_written": 0,
            "cycle": 0,
            "status_text": "skipped: TW symbol map not loaded yet",
        }

    universe_size = len(symbols)
    start_idx = await _read_cursor(universe_size)
    end_idx = min(start_idx + _BATCH_SIZE, universe_size)
    batch = symbols[start_idx:end_idx]

    rows_written = 0
    symbols_with_data = 0
    transient_errors = 0
    for i, sym in enumerate(batch):
        try:
            n = await _process_symbol(sym)
        except Exception as exc:
            # Defensive: connector should swallow transport errors
            # already. Log + continue if anything bubbles up so one
            # bad symbol doesn't sink the batch.
            transient_errors += 1
            log.warning("ingest_revenue_tw_slow.symbol_failed",
                        extra={"symbol": sym, "error": str(exc)})
            n = 0
        rows_written += n
        if n > 0:
            symbols_with_data += 1
        # Pace: skip the sleep on the last symbol of the batch.
        if i < len(batch) - 1:
            await asyncio.sleep(_INTER_SYMBOL_DELAY_SECONDS)

    # Advance cursor; wrap past universe end → bump the cycle counter.
    next_idx = end_idx if end_idx < universe_size else 0
    await _write_cursor(next_idx)
    cycle = 0
    if next_idx == 0 and start_idx > 0:
        try:
            cycle = await cache_incr(_CYCLE_KEY, ttl_seconds=365 * 24 * 3600)
        except Exception:
            cycle = 0

    pct = (end_idx / universe_size) * 100 if universe_size else 0.0
    status_text = (
        f"slice {start_idx}-{end_idx}/{universe_size} ({pct:.0f}%) · "
        f"{symbols_with_data}/{len(batch)} returned data · "
        f"{rows_written} rows upserted"
    )
    if transient_errors:
        status_text += f" · {transient_errors} transient errors"
    if cycle:
        status_text += f" · cycle #{cycle} complete"

    return {
        "symbols_in_universe": universe_size,
        "symbols_processed": len(batch),
        "symbols_with_data": symbols_with_data,
        "rows_written": rows_written,
        "transient_errors": transient_errors,
        "cycle": cycle,
        "next_cursor": next_idx,
        "status_text": status_text,
    }
