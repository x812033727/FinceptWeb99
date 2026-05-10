"""FinMind 全量回填 chain orchestration.

Replaces the host-level `/opt/finceptweb/finmind_chain.sh` with an
in-process asyncio task gated by a redis lock so the AdminPage card
can start / stop the 12-dataset 10-year backfill without SSHing in.

State lives in redis (NOT module globals) because backend runs
`uvicorn --workers 2`: GET /chain may land on a different worker
from the one running `_run_chain`, so the state must be shared.

Keys:
- ``finmind:chain:state`` — JSON blob mirroring `ChainState`, TTL 1 day
- ``finmind:chain:lock`` — SET NX EX 60, refreshed every 20 s by the
  task; expires on crash so the next Start can recover automatically
- ``finmind:chain:stop_requested`` — soft-stop flag, TTL 1 h

Host-chain coexistence: `finmind_chain.sh` still works (kept as
fallback for redeploys). When backfill_progress has recent running
rows BUT we don't hold the chain lock, we infer the host script is
running and refuse to start a parallel chain (would double-burn the
6000/hr FinMind quota).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import text

from cache.redis_cache import (
    acquire_lock,
    cache_delete,
    cache_get,
    cache_set,
    get_redis,
    key_finmind_counter,
)

log = logging.getLogger("services.finmind_chain")

# ── Redis keys ─────────────────────────────────────────────────
STATE_KEY = "finmind:chain:state"
LOCK_KEY = "finmind:chain:lock"
STOP_KEY = "finmind:chain:stop_requested"

STATE_TTL = 86_400          # 1 day — state persists across restarts
LOCK_TTL = 60               # task refreshes every 20 s
LOCK_REFRESH_INTERVAL = 20
STOP_TTL = 3600
QUOTA_COOLDOWN_SLEEP = 60   # poll quota every minute when capped
RECENT_ERRORS_KEEP = 5
STUCK_RESET_MINUTES = 15
HOST_CHAIN_RECENT_MINUTES = 10

# Same 12 datasets as `/opt/finceptweb/finmind_chain.sh` lines 53-65 —
# headline TW per-symbol datasets the operator wants in 10-year history.
DEFAULT_DATASETS: tuple[str, ...] = (
    "TaiwanStockMarginPurchaseShortSale",
    "TaiwanStockPriceAdj",
    "TaiwanStockPER",
    "TaiwanStockMarketValue",
    "TaiwanStockSecuritiesLending",
    "TaiwanDailyShortSaleBalances",
    "TaiwanStockMonthRevenue",
    "TaiwanStockBalanceSheet",
    "TaiwanStockFinancialStatements",
    "TaiwanStockCashFlowsStatement",
    "TaiwanStockDividend",
    "TaiwanStockHoldingSharesPer",
)

INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"


# ── State ──────────────────────────────────────────────────────


@dataclass
class ChainState:
    status: Literal["idle", "running", "stopping"] = "idle"
    queue: list[str] = field(default_factory=list)
    current_dataset: str | None = None
    current_symbol: str | None = None
    chunks_done: int = 0
    chunks_total: int = 0
    chunks_failed: int = 0
    started_at: str | None = None      # ISO datetime
    last_chunk_at: str | None = None
    stop_requested: bool = False
    recent_errors: list[str] = field(default_factory=list)
    # Overall progress (across ALL datasets in this chain run, not just
    # the currently-running one). selected_datasets is the chain's
    # universe at start time; universe_size is the symbol count for
    # per-symbol fan-out. total_chunks_total = universe_size *
    # len(selected_datasets) when known.
    selected_datasets: list[str] = field(default_factory=list)
    universe_size: int = 0

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, raw: str) -> "ChainState":
        data = json.loads(raw)
        return cls(**data)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def _read_state() -> ChainState:
    raw = await cache_get(STATE_KEY)
    if raw is None:
        return ChainState()
    try:
        return ChainState.from_json(raw)
    except Exception:
        log.exception("chain state JSON parse failed; resetting to idle")
        return ChainState()


async def _write_state(state: ChainState) -> None:
    await cache_set(STATE_KEY, state.to_json(), ttl_seconds=STATE_TTL)


async def _stop_requested() -> bool:
    return await cache_get(STOP_KEY) is not None


async def _refresh_lock() -> None:
    """Re-set the lock with fresh TTL. Doesn't use SET NX — we already
    hold it. Plain SET extends the TTL atomically."""
    r = await get_redis()
    await r.set(LOCK_KEY, INSTANCE_ID, ex=LOCK_TTL)


async def _release_lock() -> None:
    """Only release if we still own it. Avoids a stale task wiping a
    fresh lock taken after we lost ours to TTL expiry."""
    r = await get_redis()
    current = await r.get(LOCK_KEY)
    if current == INSTANCE_ID:
        await r.delete(LOCK_KEY)


# ── Universe + quota helpers ───────────────────────────────────


async def _load_universe() -> list[str]:
    """Pull the active TW equity universe from `tw_stock_info`. Mirrors
    `finmind_chain.sh`'s `equity.txt` source-of-truth — when the
    universe shifts (delistings, IPOs), the chain picks it up on next
    start without operator intervention."""
    from finmind.db.session import FinmindAsyncSessionLocal
    from finmind.scheduler.runner import get_universe_from_tw_stock_info

    async with FinmindAsyncSessionLocal() as session:
        return await get_universe_from_tw_stock_info(
            session, exclude_warrants=True
        )


async def _quota_used() -> int | None:
    raw = await cache_get(key_finmind_counter())
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _quota_limit_global() -> int:
    """Connector hard cap (`FINMIND_HOURLY_REQUEST_LIMIT`). Surfaced as
    its own helper so tests can monkeypatch it the same way as
    `_quota_limit` instead of reaching into `finmind_connector`."""
    from data.tw.finmind_connector import _resolve_hourly_limit

    return await _resolve_hourly_limit()


async def _quota_limit() -> int:
    """Chain pre-flight gate budget. Defaults to the global
    FINMIND_HOURLY_REQUEST_LIMIT, but operators can set
    FINMIND_CHAIN_HOURLY_BUDGET below it to reserve headroom for
    live / interactive paths (discussions etc.) — the chain stops
    at the sub-budget while the connector's hard cap stays at the
    global limit, so non-chain callers can still consume the gap.
    Falls back to the global limit if the sub-budget read fails so
    a runtime_config outage doesn't reopen the chain to the full cap."""
    global_limit = await _quota_limit_global()
    try:
        from db.session import AsyncSessionLocal
        from services.runtime_config_service import get_int as _get_int
        async with AsyncSessionLocal() as db:
            chain_budget = await _get_int(db, "FINMIND_CHAIN_HOURLY_BUDGET")
    except Exception:
        return global_limit
    # Never let chain budget exceed the connector's hard cap — that
    # would have the chain pushing past the limit only to be 4xx-ed
    # one call later.
    return min(chain_budget, global_limit)


async def _wait_for_quota(stop_check: callable) -> None:
    """Block while redis counter has overshot the hourly cap. Polls
    every minute, honours the stop flag so soft-stop works mid-wait.
    The 1-hour TTL on `finmind:daily_requests` clears the counter
    automatically; we just need to outwait it."""
    while True:
        if await stop_check():
            return
        used = await _quota_used()
        limit = await _quota_limit()
        if used is None or used < limit:
            return
        log.info(
            "chain: quota saturated %s/%s, sleeping %ss",
            used, limit, QUOTA_COOLDOWN_SLEEP,
        )
        await asyncio.sleep(QUOTA_COOLDOWN_SLEEP)


# ── Host-chain detection ───────────────────────────────────────


async def _host_chain_likely_active() -> bool:
    """`finmind_chain.sh` is a host-level nohup process invisible from
    inside the backend container. Inferred presence: there's a
    `running` chunk row from the last 10 minutes BUT we don't hold the
    chain lock. (If we held the lock, OUR task is the one creating
    those running rows.)"""
    if await cache_get(LOCK_KEY) is not None:
        return False
    from finmind.db.session import FinmindAsyncSessionLocal

    async with FinmindAsyncSessionLocal() as session:
        result = await session.execute(text(
            f"""
            SELECT count(*) FROM finmind.backfill_progress
             WHERE status='running'
               AND started_at > now() - interval '{HOST_CHAIN_RECENT_MINUTES} minutes'
            """
        ))
        return (result.scalar() or 0) > 0


# ── Stuck-chunk cleanup ────────────────────────────────────────


async def reset_stuck() -> int:
    """Flip `status='running'` rows older than STUCK_RESET_MINUTES back
    to `pending` so the chain re-picks them on next iteration. Returns
    the number of rows reset."""
    from finmind.db.session import FinmindAsyncSessionLocal

    async with FinmindAsyncSessionLocal() as session:
        result = await session.execute(text(
            f"""
            UPDATE finmind.backfill_progress
               SET status='pending',
                   error_message='reset_stuck via /api/admin/finmind/chain',
                   started_at=NULL,
                   finished_at=NULL
             WHERE status='running'
               AND started_at < now() - interval '{STUCK_RESET_MINUTES} minutes'
            """
        ))
        await session.commit()
        return result.rowcount or 0


# ── Per-dataset chunk count helpers ────────────────────────────


async def _count_progress_for_dataset(
    dataset_code: str,
) -> tuple[int, int, int]:
    """Return (done, failed, total) row counts in
    `finmind.backfill_progress` for one dataset. `total` covers every
    status (done + failed + pending + running + skipped), so it's a
    safe denominator for `done/total` and never lets the bar exceed
    100% — unlike `len(universe)`, which only counts symbols and
    misses the multi-chunk-per-symbol rows that `run_due` cron + chain
    UI both write."""
    from finmind.db.session import FinmindAsyncSessionLocal

    async with FinmindAsyncSessionLocal() as session:
        result = await session.execute(text(
            """
            SELECT
              count(*) FILTER (WHERE status='done')   AS done,
              count(*) FILTER (WHERE status='failed') AS failed,
              count(*)                                AS total
              FROM finmind.backfill_progress
             WHERE dataset_code=:ds
            """
        ), {"ds": dataset_code})
        row = result.first()
        return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))


async def _count_progress_for_datasets(
    datasets: list[str],
) -> tuple[int, int]:
    """Return (done, total) summed across the given datasets. Powers
    the overall progress bar — answers `已完成 X / 預期 Y` for the
    whole chain, not just the current dataset. `total` is the ledger
    row count, not `universe_size × len(datasets)`, so the bar stays
    bounded by what's actually scheduled."""
    if not datasets:
        return (0, 0)
    from finmind.db.session import FinmindAsyncSessionLocal

    async with FinmindAsyncSessionLocal() as session:
        result = await session.execute(text(
            """
            SELECT
              count(*) FILTER (WHERE status='done') AS done,
              count(*)                              AS total
              FROM finmind.backfill_progress
             WHERE dataset_code = ANY(:ds)
            """
        ), {"ds": list(datasets)})
        row = result.first()
        return (int(row[0] or 0), int(row[1] or 0))


async def _per_dataset_progress(
    datasets: list[str], universe_size: int,
) -> list[dict]:
    """Per-dataset rollup for the AdminPage card: chunks done/failed/
    pending/running and an *estimated* row count for the destination
    table.

    Two subtleties baked into the numbers:

    - `chunks_total` is the sum of all backfill_progress rows for that
      dataset, NOT `universe_size`. Single-day datasets emit one chunk
      per (symbol, date) so the row count outpaces the symbol count;
      using `universe_size` would put `chunks_done > chunks_total`
      and give a misleading >100% bar.
    - Most finmind tables are TimescaleDB hypertables, where the
      parent's `pg_class.reltuples` is always -1 (data lives in
      chunks). `approximate_row_count()` walks the chunks and works
      for both hypertables and plain tables, so we use it
      uniformly. Estimate-grade — accurate enough for the progress
      meter and cheap enough to call on every 3 s poll."""
    if not datasets:
        return []
    from finmind.dataset_catalog import all_entries
    from finmind.db.session import FinmindAsyncSessionLocal

    table_map = {
        entry.dataset_code: entry.local_table
        for _, entry in all_entries()
    }

    async with FinmindAsyncSessionLocal() as session:
        chunks_rows = await session.execute(text(
            """
            SELECT dataset_code,
                   count(*) FILTER (WHERE status='done')    AS done,
                   count(*) FILTER (WHERE status='failed')  AS failed,
                   count(*) FILTER (WHERE status='pending') AS pending,
                   count(*) FILTER (WHERE status='running') AS running,
                   count(*)                                 AS total
              FROM finmind.backfill_progress
             WHERE dataset_code = ANY(:ds)
             GROUP BY dataset_code
            """
        ), {"ds": list(datasets)})
        chunks_by_ds = {
            row[0]: {
                "done":    int(row[1] or 0),
                "failed":  int(row[2] or 0),
                "pending": int(row[3] or 0),
                "running": int(row[4] or 0),
                "total":   int(row[5] or 0),
            }
            for row in chunks_rows
        }

        local_tables = sorted({
            table_map.get(ds, "") for ds in datasets
        } - {""})
        row_counts: dict[str, int] = {}
        # Table names come from the static dataset_catalog, not user
        # input — safe to interpolate. (asyncpg can't bind a regclass
        # parameter so we have to format the SQL ourselves.) Belt-and-
        # suspenders: only allow [a-z0-9_] to forbid stray quoting.
        import re
        for tbl in local_tables:
            if not re.fullmatch(r"[a-z0-9_]+", tbl):
                continue
            try:
                result = await session.execute(text(
                    f"SELECT approximate_row_count('finmind.{tbl}'::regclass)"
                ))
                count = result.scalar()
                if count is not None:
                    row_counts[tbl] = max(0, int(count))
            except Exception:
                # Table doesn't exist yet (Phase 1 migration not landed)
                # or regclass cast failed — skip; UI shows "—".
                pass

    out = []
    for ds in datasets:
        local_table = table_map.get(ds, "") or None
        c = chunks_by_ds.get(ds, {
            "done": 0, "failed": 0, "pending": 0, "running": 0, "total": 0,
        })
        # Fall back to universe_size for datasets that have no
        # backfill_progress rows yet (chain hasn't started picking
        # them up) — the user still wants a sensible denominator.
        chunks_total = c["total"] or universe_size
        out.append({
            "dataset": ds,
            "local_table": local_table,
            "chunks_done":    c["done"],
            "chunks_failed":  c["failed"],
            "chunks_pending": c["pending"],
            "chunks_running": c["running"],
            "chunks_total":   chunks_total,
            "row_count":      row_counts.get(local_table) if local_table else None,
        })
    return out


# ── Public API ─────────────────────────────────────────────────


async def _heal_if_orphaned(state: ChainState) -> ChainState:
    """If state claims active but no worker holds the lock, the owner
    process died (container restart, OOM, host reboot) without running
    `_run_chain`'s finally block. Reset to idle so the UI doesn't hang
    on `stopping` forever. Lock TTL is 60 s and the worker refreshes
    every 20 s, so a missing lock means the worker is genuinely gone,
    not transiently stalled."""
    if state.status not in ("running", "stopping"):
        return state
    r = await get_redis()
    if await r.get(LOCK_KEY) is not None:
        return state
    log.warning(
        "chain: orphaned state detected (status=%s, no lock); resetting to idle",
        state.status,
    )
    state.status = "idle"
    state.current_dataset = None
    state.current_symbol = None
    state.queue = []
    state.stop_requested = False
    await _write_state(state)
    await cache_delete(STOP_KEY)
    return state


async def get_state() -> dict:
    """Read state + augment with live quota gauge, host-chain
    inference, and overall progress. Returns a JSON-serialisable dict
    so the FastAPI handler can return it directly."""
    state = await _read_state()
    state = await _heal_if_orphaned(state)
    used = await _quota_used()
    limit = await _quota_limit()
    global_limit = await _quota_limit_global()
    state.stop_requested = await _stop_requested()
    # Overall progress across ALL datasets in this chain run. When the
    # chain is idle and selected_datasets is empty, both totals are 0
    # — the frontend renders "—" instead of "0/0" in that case. The
    # denominator is the ledger row count (sum of all backfill_progress
    # rows for the selected datasets), not `universe_size × len(...)`,
    # so the bar stays bounded by what's actually scheduled and never
    # exceeds 100% even when the cron `run_due` writer has produced
    # multi-chunk-per-symbol rows.
    total_done, total_total = await _count_progress_for_datasets(
        state.selected_datasets
    )
    # Per-dataset live counters: the redis-persisted `state.chunks_*`
    # baseline is `len(universe)` (symbols), but `chunks_done` is
    # incremented from the historical-cumulative `done` count in the
    # ledger, which can outpace symbol count and produce >100%. Read
    # fresh from the ledger when a dataset is currently active so the
    # bar always agrees with reality. When idle (no current_dataset)
    # we zero out so the frontend's `chunks_total > 0 &&` guard hides
    # the live bar — otherwise a stale-but-paradoxical 127933/126465
    # lingers across chain runs (the trigger for the 2026-05-10 fix).
    state_dict = {**state.__dict__}
    if (
        state.status in ("running", "stopping")
        and state.current_dataset
    ):
        cur_done, cur_failed, cur_total = (
            await _count_progress_for_dataset(state.current_dataset)
        )
        state_dict["chunks_done"] = cur_done
        state_dict["chunks_failed"] = cur_failed
        state_dict["chunks_total"] = cur_total
    else:
        state_dict["chunks_done"] = 0
        state_dict["chunks_failed"] = 0
        state_dict["chunks_total"] = 0
    per_dataset = await _per_dataset_progress(
        state.selected_datasets, state.universe_size,
    )
    return {
        **state_dict,
        "quota_used": used,
        "quota_limit": limit,
        "quota_limit_global": global_limit,
        "external_activity_detected": await _host_chain_likely_active(),
        "default_datasets": list(DEFAULT_DATASETS),
        "total_chunks_done": total_done,
        "total_chunks_total": total_total,
        "per_dataset_progress": per_dataset,
    }


async def start_chain(
    datasets: list[str], days: int = 1095, reset_stuck_first: bool = True,
) -> dict:
    """Start the chain. Returns the new state dict.

    Raises ChainAlreadyRunning when the redis lock is held. Caller
    (FastAPI handler) translates to HTTP 409.
    """
    if not datasets:
        raise ValueError("datasets must be non-empty")
    if await _host_chain_likely_active():
        raise ChainConflict(
            "host-level finmind_chain.sh appears to be running. Stop "
            "that first (kill the nohup process), then retry."
        )
    got = await acquire_lock(LOCK_KEY, LOCK_TTL, value=INSTANCE_ID)
    if not got:
        raise ChainAlreadyRunning("chain already running on another worker")

    if reset_stuck_first:
        n_reset = await reset_stuck()
        if n_reset:
            log.info("chain start: reset %s stuck chunks", n_reset)
    await cache_delete(STOP_KEY)

    state = ChainState(
        status="running",
        queue=list(datasets),
        current_dataset=None,
        current_symbol=None,
        chunks_done=0,
        chunks_total=0,
        chunks_failed=0,
        started_at=_now_iso(),
        last_chunk_at=None,
        stop_requested=False,
        recent_errors=[],
        selected_datasets=list(datasets),
        universe_size=0,    # filled in by _run_chain after _load_universe
    )
    await _write_state(state)

    asyncio.create_task(_run_chain(datasets, days), name="finmind_chain")
    return await get_state()


async def stop_chain() -> dict:
    """Set the soft-stop flag. The running task observes it after the
    current chunk and exits gracefully. Idempotent."""
    await cache_set(STOP_KEY, "1", ttl_seconds=STOP_TTL)
    state = await _read_state()
    if state.status == "running":
        state.status = "stopping"
        state.stop_requested = True
        await _write_state(state)
    return await get_state()


# ── Run loop (single owner) ────────────────────────────────────


async def _run_chain(datasets: list[str], days: int) -> None:
    """Runs in the worker that won the lock. Iterates datasets ×
    universe, calls ingest_chunk per (dataset, symbol). Honours stop
    flag between chunks. Never raises — terminal exceptions are
    appended to recent_errors and the chain moves on."""
    from datetime import date, timedelta

    from finmind.db.session import FinmindAsyncSessionLocal
    from finmind.ingest.runner import ingest_chunk

    end = date.today()
    start = end - timedelta(days=days)
    last_lock_refresh = datetime.now(tz=timezone.utc)

    try:
        try:
            universe = await _load_universe()
        except Exception as exc:
            log.exception("chain: universe load failed")
            state = await _read_state()
            state.recent_errors = (
                [f"universe load failed: {exc!r}"] + state.recent_errors
            )[:RECENT_ERRORS_KEEP]
            state.status = "idle"
            await _write_state(state)
            return
        if not universe:
            state = await _read_state()
            state.recent_errors = (
                ["tw_stock_info empty — enable + run TaiwanStockInfo first"]
                + state.recent_errors
            )[:RECENT_ERRORS_KEEP]
            state.status = "idle"
            await _write_state(state)
            return

        # Persist universe_size so get_state() can compute the overall
        # progress denominator (universe_size × len(selected_datasets)).
        state = await _read_state()
        state.universe_size = len(universe)
        await _write_state(state)

        for ds in datasets:
            if await _stop_requested():
                break

            done0, failed0, total0 = await _count_progress_for_dataset(ds)
            state = await _read_state()
            state.current_dataset = ds
            state.current_symbol = None
            state.chunks_done = done0
            state.chunks_failed = failed0
            # Ledger row count, not len(universe) — multi-chunk-per-
            # symbol rows produced by the cron `run_due` writer would
            # otherwise push done past total. Floor to len(universe)
            # so a brand-new dataset with empty backfill_progress
            # still shows a sensible denominator until the first chunks
            # land. get_state() re-queries this on every poll so any
            # mid-run drift (new pending rows getting created) is
            # corrected on the read path within ~3 s.
            state.chunks_total = max(total0, len(universe))
            await _write_state(state)

            for symbol in universe:
                if await _stop_requested():
                    break

                # Refresh lock if it's been ~LOCK_REFRESH_INTERVAL since
                # last refresh (cheap; avoids a lock that expired mid-run
                # while a slow chunk was in flight).
                now = datetime.now(tz=timezone.utc)
                if (now - last_lock_refresh).total_seconds() >= LOCK_REFRESH_INTERVAL:
                    await _refresh_lock()
                    last_lock_refresh = now

                # Pre-flight quota gate. Avoid hammering FinMind with
                # already-doomed chunks once the hourly cap is hit —
                # they all return `failed (FinMindQuotaExhausted)` and
                # the same chunk would just be retried on the next
                # chain run anyway. Cleaner: wait for quota reset.
                await _wait_for_quota(_stop_requested)
                if await _stop_requested():
                    break

                async with FinmindAsyncSessionLocal() as session:
                    try:
                        result = await ingest_chunk(
                            session,
                            dataset_code=ds,
                            symbol=symbol,
                            range_start=start,
                            range_end=end,
                        )
                    except Exception as exc:
                        # ingest_chunk catches its own exceptions, but a
                        # session-level error (lost DB connection) can
                        # still bubble up. Don't let it kill the chain.
                        log.exception(
                            "chain: ingest_chunk raised for %s %s",
                            ds, symbol,
                        )
                        state = await _read_state()
                        state.chunks_failed += 1
                        state.recent_errors = (
                            [f"{ds} {symbol}: {exc!r}"] + state.recent_errors
                        )[:RECENT_ERRORS_KEEP]
                        state.current_symbol = symbol
                        state.last_chunk_at = _now_iso()
                        await _write_state(state)
                        continue

                state = await _read_state()
                state.current_symbol = symbol
                state.last_chunk_at = _now_iso()
                if result.status == "done":
                    state.chunks_done += 1
                elif result.status == "failed":
                    state.chunks_failed += 1
                    if result.error:
                        state.recent_errors = (
                            [f"{ds} {symbol}: {result.error}"]
                            + state.recent_errors
                        )[:RECENT_ERRORS_KEEP]
                # 'skipped' isn't counted toward either bar — they're
                # legitimately empty chunks (no trading days in range).
                await _write_state(state)
        # natural completion or stop flag — fall through to finally
    finally:
        state = await _read_state()
        state.status = "idle"
        state.current_dataset = None
        state.current_symbol = None
        state.queue = []
        state.stop_requested = False
        await _write_state(state)
        await cache_delete(STOP_KEY)
        await _release_lock()
        log.info("chain: terminated, lock released")


# ── Errors ─────────────────────────────────────────────────────


class ChainAlreadyRunning(RuntimeError):
    pass


class ChainConflict(RuntimeError):
    """Raised when an external chain (host nohup) is detected."""
    pass
