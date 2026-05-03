"""Phase A backfill runner.

Glues `data.tw.finmind_connector` (upstream HTTP) → `mappings.py`
(row transform) → local-table UPSERT → `progress.py` (chunk
ledger) → `dataset_sources` telemetry update.

Source-agnostic by design — the `SourceClient` Protocol means a
Phase B TWSE / TPEX connector implementing the same shape can
swap in by changing `dataset_sources.active_source` per row, no
runner code change.

Public entry point:

    await ingest_chunk(
        session,
        dataset_code='TaiwanStockPrice',
        symbol='2330',
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )

Returns a `ChunkResult(rows_written, status, error)`. Caller
schedules / parallelizes — this runner does one chunk per call so
the rate-limiting / scheduling stays in the orchestrator (CLI or
APScheduler).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.ingest.mappings import (
    DatasetMapping,
    MappingNotFoundError,
    find_mapping,
    transform_row,
)
from finmind.ingest.progress import (
    claim_chunk,
    fail_chunk,
    finish_chunk,
    skip_chunk,
)
from finmind.models.dataset_source import DatasetSource

log = logging.getLogger("finmind.ingest")


@dataclass
class ChunkResult:
    status: str           # 'done' | 'failed' | 'skipped'
    rows_written: int
    error: str | None


class SourceClient(Protocol):
    """Pluggable upstream interface. The Phase A FinMind client and
    every Phase B self-crawl client implements this — runner doesn't
    care which."""

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        ...


class FinmindClient:
    """Wraps `data.tw.finmind_connector._query` to satisfy
    `SourceClient`. Late-imports the connector so tests can run the
    runner without httpx in the import chain."""

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        from data.tw.finmind_connector import _query

        return await _query(
            dataset_code,
            symbol or "",
            start_date.isoformat(),
            end_date.isoformat(),
        )


def _build_upsert_sql(
    table: str, columns: list[str], pk_columns: tuple[str, ...]
) -> str:
    """Build a dialect-portable UPSERT statement.

    Postgres + SQLite both support ON CONFLICT (...) DO UPDATE SET ...
    with the same syntax, so a single SQL string works in tests and
    prod. The non-PK columns get overwritten — which is the right
    semantic for FinMind data because corrections to e.g. a 2-year-old
    revenue figure should land in the local table on re-ingest.
    """
    placeholders = ", ".join(f":{c}" for c in columns)
    cols_csv = ", ".join(columns)
    pk_csv = ", ".join(pk_columns)
    update_cols = [c for c in columns if c not in pk_columns]
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    return (
        f"INSERT INTO {table} ({cols_csv}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT ({pk_csv}) DO UPDATE SET {update_set}"
    )


def _coerce_for_binding(value: Any) -> Any:
    """SQLite's binary protocol rejects `decimal.Decimal` — round-trip
    via str preserves precision in both Postgres NUMERIC and SQLite
    TEXT columns, and the SQLAlchemy type coercion on read returns
    Decimal again on Postgres. (PG's asyncpg accepts Decimal natively
    so this no-op is harmless there.)"""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    return value


async def _upsert_rows(
    session: AsyncSession, mapping: DatasetMapping, rows: list[dict[str, Any]]
) -> int:
    """UPSERT the (already-transformed) rows into mapping.local_table.

    Trusts that every row has the same keys — true after
    `transform_row` runs since it produces a uniform shape per
    dataset.
    """
    if not rows:
        return 0

    columns = list(rows[0].keys())
    sql = _build_upsert_sql(mapping.local_table, columns, mapping.pk_columns)

    bound_rows = [
        {k: _coerce_for_binding(v) for k, v in row.items()}
        for row in rows
    ]
    # executemany — every row has the same keys, single round-trip.
    await session.execute(text(sql), bound_rows)
    return len(rows)


async def _update_dataset_telemetry(
    session: AsyncSession,
    dataset_code: str,
    rows_written: int,
    error: str | None,
) -> None:
    """Mirror the chunk outcome onto `dataset_sources` so the headline
    `last_ingest_at` / `last_error` columns reflect reality without
    the admin UI having to scan `backfill_progress`.

    Uses Core UPDATE rather than ORM attribute mutation — same reason
    as `progress._set_chunk_status`: avoids MissingGreenlet from
    expired-attribute lazy refresh on the failure path."""
    from sqlalchemy import update

    await session.execute(
        update(DatasetSource)
        .where(DatasetSource.dataset_code == dataset_code)
        .values(
            last_ingest_at=datetime.now(tz=timezone.utc),
            last_ingest_rows=rows_written,
            last_error=(error[:1000] if error else None),
        )
    )
    await session.commit()


async def ingest_chunk(
    session: AsyncSession,
    *,
    dataset_code: str,
    symbol: str | None,
    range_start: date,
    range_end: date,
    client: SourceClient | None = None,
) -> ChunkResult:
    """Drive one (dataset, symbol, range) chunk end-to-end.

    Idempotent — re-running the same chunk re-claims, re-fetches,
    UPSERTs (overwriting any drift), and re-records progress. Safe
    to call from a worker pool; each call is its own transaction.
    """
    # 1. Resolve dataset_sources row → which source + which table.
    ds = await session.get(DatasetSource, dataset_code)
    if ds is None:
        return ChunkResult(
            "skipped", 0, f"dataset_code {dataset_code} not in dataset_sources"
        )
    if not ds.local_table:
        return ChunkResult(
            "skipped", 0,
            f"{dataset_code}: no local_table — schema not built yet",
        )

    source = ds.active_source

    # 2. Resolve mapping. Missing mapping = skipped (Phase 1 schema
    # outpaced Phase 2 mapping work).
    try:
        mapping = find_mapping(dataset_code)
    except MappingNotFoundError as exc:
        chunk = await claim_chunk(
            session,
            dataset_code=dataset_code,
            symbol=symbol,
            source=source,
            range_start=range_start,
            range_end=range_end,
        )
        await skip_chunk(session, chunk.id, str(exc))
        await _update_dataset_telemetry(session, dataset_code, 0, str(exc))
        return ChunkResult("skipped", 0, str(exc))

    # 3. Claim chunk in 'running' state.
    chunk = await claim_chunk(
        session,
        dataset_code=dataset_code,
        symbol=symbol,
        source=source,
        range_start=range_start,
        range_end=range_end,
    )

    # 4. Fetch + transform + UPSERT.
    #
    # Rollback ONLY when the UPSERT itself raised — a fetch / transform
    # error didn't begin a DB transaction and rolling back here would
    # expire ORM-cached chunk + DS objects, which then trips
    # MissingGreenlet on subsequent attribute access. Keeping the
    # rollback narrowly scoped to the SQL phase preserves the session
    # for the failure-recording path.
    if client is None:
        # Source resolution: 'finmind' → FinmindClient; anything else
        # routes through `selfcrawl.resolve_client` so the Phase A → B
        # switch is a UPDATE on `dataset_sources.active_source`, not a
        # code change here.
        from finmind.ingest.selfcrawl import resolve_client

        upstream = resolve_client(source)
    else:
        upstream = client
    try:
        raw_rows = await upstream.fetch(
            dataset_code, symbol, range_start, range_end
        )
        transformed = [transform_row(r, mapping) for r in raw_rows]
        # Drop rows whose PK columns are NULL — corrupt upstream data
        # otherwise raises a constraint violation that aborts the
        # transaction and leaves the chunk stuck running.
        transformed = [
            r for r in transformed
            if all(r.get(pk) is not None for pk in mapping.pk_columns)
        ]
    except Exception as exc:
        log.exception("ingest_chunk fetch/transform failed: %s %s",
                      dataset_code, symbol)
        await fail_chunk(session, chunk.id, repr(exc))
        await _update_dataset_telemetry(session, dataset_code, 0, repr(exc))
        return ChunkResult("failed", 0, repr(exc))

    try:
        rows_written = await _upsert_rows(session, mapping, transformed)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        log.exception("ingest_chunk upsert failed: %s %s",
                      dataset_code, symbol)
        await fail_chunk(session, chunk.id, repr(exc))
        await _update_dataset_telemetry(session, dataset_code, 0, repr(exc))
        return ChunkResult("failed", 0, repr(exc))

    await finish_chunk(session, chunk.id, rows_written)
    await _update_dataset_telemetry(session, dataset_code, rows_written, None)
    return ChunkResult("done", rows_written, None)


async def list_enabled_datasets(session: AsyncSession) -> list[DatasetSource]:
    """Datasets the runner is allowed to ingest right now.

    `enabled=True` opt-in + non-empty `local_table` (schema exists).
    Caller iterates and calls `ingest_chunk` for each; ordering is the
    caller's choice (priority queue, daily cron, etc.)."""
    rows = (
        await session.execute(
            select(DatasetSource)
            .where(DatasetSource.enabled.is_(True))
            .where(DatasetSource.local_table != "")
        )
    ).scalars().all()
    return list(rows)
