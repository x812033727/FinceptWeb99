"""Auto-runner — DB-aware orchestration on top of the pure dispatcher.

`run_due_now` is the only public entry point: read all enabled
DatasetSource rows, ask `dispatcher.expand_due_datasets` what's due,
loop and call `ingest_chunk` for each. Returns a per-chunk result
list so the CLI can print + the eventual /admin endpoint can render.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.ingest.runner import ChunkResult, ingest_chunk
from finmind.models.dataset_source import DatasetSource
from finmind.models.master import TwStockInfo
from finmind.scheduler.dispatcher import DueChunk, expand_due_datasets

log = logging.getLogger("finmind.scheduler")


async def get_universe_from_tw_stock_info(
    session: AsyncSession,
    *,
    market: str | None = None,
    exclude_warrants: bool = True,
) -> list[str]:
    """Read the active per-symbol universe from `tw_stock_info`.

    Once TaiwanStockInfo is enabled and ingest has run at least once,
    this helper replaces the hand-maintained `--symbols-file` path —
    the cron picks up new listings + drops delisted ones automatically
    without operator intervention. Returns symbols sorted for
    deterministic ordering (so logs / progress are stable).

    Filters:
      - `market`: 'TWSE' / 'OTC' / None (all). Per the master.py
        schema, `market` is the upstream market code as normalized
        by the FinMind / TWSE wrappers.
      - `exclude_warrants`: True drops `is_warrant=true` rows so the
        usual equity-only daily cron doesn't fan out across warrants
        (which dwarf the equity universe in row count).

    Returns an empty list when the table is empty — caller decides
    whether to fall back to a hardcoded universe or skip the per-
    symbol datasets entirely (`--skip-per-symbol`)."""
    stmt = select(TwStockInfo.symbol)
    if market is not None:
        stmt = stmt.where(TwStockInfo.market == market)
    if exclude_warrants:
        stmt = stmt.where(TwStockInfo.is_warrant.is_(False))
    stmt = stmt.order_by(TwStockInfo.symbol)

    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


@dataclass
class ChunkOutcome:
    chunk: DueChunk
    result: ChunkResult


async def run_due_now(
    session: AsyncSession,
    *,
    symbols: list[str] | None = None,
    now: datetime | None = None,
    client=None,
) -> list[ChunkOutcome]:
    """One sweep of the due-dataset graph.

    Caller-supplied `symbols` is the per-symbol universe; market-wide
    datasets ignore it. `now` defaults to UTC wall clock. `client`
    overrides the ingest source (for tests) — production leaves it
    None and the runner picks per `dataset_sources.active_source`.

    Failures don't short-circuit: one bad dataset still lets the rest
    run. The per-chunk telemetry already lands in `dataset_sources`
    via `_update_dataset_telemetry`, so the next status report shows
    exactly what failed.
    """
    now = now or datetime.now(tz=timezone.utc)

    rows = (
        await session.execute(
            select(DatasetSource).where(DatasetSource.enabled.is_(True))
        )
    ).scalars().all()

    chunks = expand_due_datasets(list(rows), now, symbols=symbols)
    log.info(
        "scheduler: %d enabled datasets, %d due chunks", len(rows), len(chunks)
    )

    outcomes: list[ChunkOutcome] = []
    for chunk in chunks:
        try:
            result = await ingest_chunk(
                session,
                dataset_code=chunk.dataset_code,
                symbol=chunk.symbol,
                range_start=chunk.range_start,
                range_end=chunk.range_end,
                client=client,
            )
        except Exception as exc:
            # ingest_chunk catches its own errors and returns a
            # ChunkResult — a raise here means something deeper broke
            # (DB connection lost, etc.). Surface as a failed-result
            # in the outcome list so the caller sees the whole sweep
            # outcome rather than crashing on the bad chunk.
            log.exception(
                "scheduler crashed on %s/%s",
                chunk.dataset_code, chunk.symbol or "<market-wide>",
            )
            result = ChunkResult(
                status="failed", rows_written=0, error=repr(exc),
            )
        outcomes.append(ChunkOutcome(chunk=chunk, result=result))

    return outcomes
