"""Due-detection logic — pure function over (datasets, now).

The single tuning knob is `_BUDGET` per `ingest_freq` value. Each
budget is the maximum age `last_ingest_at` is allowed to reach
before the dataset is "due"; matches the staleness budgets in
`finmind/scripts/check.py` so the cron and the alarm see the same
notion of "late".

Two design choices worth pinning:

  1. NULL last_ingest_at counts as due — first-run after a deploy
     should fire immediately, not wait for the cadence to elapse.
  2. `realtime` and `on_demand` cadences NEVER schedule. Realtime
     datasets aren't persisted (see `dataset_catalog.py`); on-demand
     datasets only run when the operator manually triggers them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from finmind.models.dataset_source import DatasetSource


# Per-cadence budget — how stale `last_ingest_at` is allowed to get
# before we re-run. Slightly larger than the natural cadence so a
# one-cycle blip doesn't double-fire when the cron catches up.
_BUDGET: dict[str, timedelta] = {
    "daily":     timedelta(hours=22),    # ~daily, allow 2h grace
    "weekly":    timedelta(days=6),
    "monthly":   timedelta(days=28),
    "quarterly": timedelta(days=88),
}

# Default backfill range when a dataset becomes due. Tuned per cadence:
# daily backfills the last 7 days (catches weekend recovery + late
# corrections); monthly backfills the last 90 days (covers late filers).
_RANGE_DAYS: dict[str, int] = {
    "daily":     7,
    "weekly":    14,
    "monthly":   90,
    "quarterly": 180,
}


@dataclass(frozen=True)
class DueChunk:
    """One unit of work the runner should execute. `symbol=None`
    means market-wide call; for per-symbol datasets the runner fans
    out across a caller-supplied symbol universe and produces one
    DueChunk per (dataset, symbol)."""

    dataset_code: str
    local_table: str
    active_source: str
    per_symbol: bool
    symbol: str | None
    range_start: date
    range_end: date


def is_due(
    ingest_freq: str,
    last_ingest_at: datetime | None,
    now: datetime,
) -> bool:
    """Pure check: is this dataset's last_ingest_at stale relative to
    its cadence?"""
    if ingest_freq in ("realtime", "on_demand"):
        return False
    budget = _BUDGET.get(ingest_freq)
    if budget is None:
        # Unknown cadence — refuse to schedule (even when never run)
        # rather than silently firing every poll. Operator notices the
        # dataset never auto-runs and either adds the cadence to
        # _BUDGET or changes ingest_freq to a known value. Checked
        # before the NULL-last_ingest_at branch so unknown cadences
        # don't accidentally fire on first deploy.
        return False
    if last_ingest_at is None:
        # First-run after a deploy should fire immediately, not wait
        # for the cadence to elapse.
        return True
    if last_ingest_at.tzinfo is None:
        last_ingest_at = last_ingest_at.replace(tzinfo=timezone.utc)
    return (now - last_ingest_at) > budget


def suggested_range(
    ingest_freq: str, now: datetime
) -> tuple[date, date]:
    """Default backfill window for a freshly-due dataset. Conservative:
    overlapping a few days with the previous run is harmless because
    UPSERT is idempotent, but missing a window is silent data loss."""
    end = now.date()
    days = _RANGE_DAYS.get(ingest_freq, 7)
    return end - timedelta(days=days), end


# Datasets whose `data_id` is a warrant code (not an equity stock_id).
# When fanning per-symbol, these datasets read from the warrant
# universe (`is_warrant=true` rows in `tw_stock_info`) instead of the
# default equity universe. Kept as an explicit set rather than a name
# heuristic because `TaiwanStockInfoWithWarrant` *is* market-wide and
# would be misclassified by a substring match.
_WARRANT_UNIVERSE_DATASETS: frozenset[str] = frozenset({
    "TaiwanStockWarrantTradingDailyReport",
})


def expand_due_datasets(
    datasets: list[DatasetSource],
    now: datetime,
    *,
    symbols: list[str] | None = None,
    warrant_symbols: list[str] | None = None,
) -> list[DueChunk]:
    """Pure expansion — turns enabled+due dataset rows into a flat
    work list of DueChunks the runner can iterate.

    `symbols` is the equity universe to fan default per-symbol datasets
    across. `warrant_symbols` is the warrant universe used by datasets
    listed in `_WARRANT_UNIVERSE_DATASETS`. When the relevant universe
    is None, per-symbol datasets are returned with `symbol=None` (the
    runner is responsible for handling that — most upstream sources
    will fail without a symbol; the chunk gets recorded as failed
    with a clear error). When the universe is non-empty, one DueChunk
    is emitted per (dataset, symbol).

    Market-wide datasets ignore both universes and emit one chunk each.
    """
    chunks: list[DueChunk] = []
    for ds in datasets:
        if not ds.enabled or not ds.local_table:
            continue
        if not is_due(ds.ingest_freq, ds.last_ingest_at, now):
            continue

        rs, re = suggested_range(ds.ingest_freq, now)

        # Single-day datasets get one chunk per day inside the suggested
        # range, instead of one multi-day chunk that the runner would
        # internally split. This makes ledger granularity per-day —
        # retries reach individual days, status reports show a 7-of-30
        # progress bar instead of 1 catch-all chunk that's "running"
        # while internally on day 4 of 30. The flag was also mirrored
        # into the runner's mapping layer for callers that bypass the
        # scheduler (CLI backfill, direct ingest_chunk).
        if getattr(ds, "single_day", False):
            range_starts = []
            cur = rs
            one = timedelta(days=1)
            while cur <= re:
                range_starts.append(cur)
                cur += one
        else:
            range_starts = [rs]

        if ds.per_symbol:
            universe = (
                warrant_symbols
                if ds.dataset_code in _WARRANT_UNIVERSE_DATASETS
                else symbols
            )
            if universe:
                for sym in universe:
                    for start_day in range_starts:
                        end_day = start_day if getattr(ds, "single_day", False) else re
                        chunks.append(DueChunk(
                            dataset_code=ds.dataset_code,
                            local_table=ds.local_table,
                            active_source=ds.active_source,
                            per_symbol=True,
                            symbol=sym,
                            range_start=start_day,
                            range_end=end_day,
                        ))
                continue
        for start_day in range_starts:
            end_day = start_day if getattr(ds, "single_day", False) else re
            chunks.append(DueChunk(
                dataset_code=ds.dataset_code,
                local_table=ds.local_table,
                active_source=ds.active_source,
                per_symbol=ds.per_symbol,
                symbol=None,
                range_start=start_day,
                range_end=end_day,
            ))
    return chunks
