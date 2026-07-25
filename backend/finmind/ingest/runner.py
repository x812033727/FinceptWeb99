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
from finmind.redaction import redact_exception, redact_secret_text

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
    runner without httpx in the import chain.

    All calls run inside `quota_strict()` so an hourly-cap overrun
    raises `FinMindQuotaExhausted` and the chunk records as `failed`,
    not `done (0 rows)`. Without this, the live-serving fallback path
    (return `[]` on overrun) would silently let `run_due` mark a chunk
    fresh when there's actually data we couldn't fetch.
    """

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        from datetime import timedelta

        from data.tw.finmind_connector import _query, quota_strict
        from finmind.ingest.mappings import MAPPINGS

        mapping = MAPPINGS.get(dataset_code)
        with quota_strict():
            if mapping is not None and mapping.single_day:
                # Day-by-day fan-out: FinMind rejects multi-day queries on
                # KBar / PriceTick / BlockTradingDailyReport /
                # GovernmentBankBuySell with HTTP 400. Iterate dates and
                # concatenate per-day responses; omit end_date so FinMind's
                # validator doesn't reject the request.
                rows: list[dict[str, Any]] = []
                cursor = start_date
                one = timedelta(days=1)
                while cursor <= end_date:
                    day_rows = await _query(
                        dataset_code,
                        symbol or "",
                        cursor.isoformat(),
                        None,
                    )
                    rows.extend(day_rows)
                    cursor += one
                return rows

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
    """Both DBAPIs (asyncpg, aiosqlite) reject some Python types when
    bound through raw `text()` queries:

      - `decimal.Decimal` → SQLite has no native Decimal support; we
        round-trip via str which preserves precision in PG NUMERIC
        and SQLite TEXT alike.
      - `dict` / `list` (for JSON / JSONB columns) → SQLite needs a
        JSON string. PG's asyncpg accepts native Python objects when
        the column type is registered, but raw `text()` bypasses that
        so we serialize for both.
    """
    import json
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str, ensure_ascii=False)
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

    safe_error = redact_secret_text(error)
    await session.execute(
        update(DatasetSource)
        .where(DatasetSource.dataset_code == dataset_code)
        .values(
            last_ingest_at=datetime.now(tz=timezone.utc),
            last_ingest_rows=rows_written,
            last_error=(safe_error[:1000] if safe_error else None),
        )
    )
    await session.commit()


def _is_permanent_client_error(exc: BaseException) -> bool:
    """4xx other than 429: the request shape is rejected — retrying the
    same source is pointless, but a different source may serve it."""
    import httpx

    return (
        isinstance(exc, httpx.HTTPStatusError)
        and 400 <= exc.response.status_code < 500
        and exc.response.status_code != 429
    )


def _resolve_fallback_client(dataset_code: str, source: str):
    """The SourceClient for this dataset's catalog fallback, or None.

    Only meaningful when the PRIMARY source raised: a dataset already
    running on its fallback (active_source != 'finmind') has nowhere
    further to go.
    """
    if source != "finmind":
        return None
    from finmind.dataset_catalog import fallback_source_for
    from finmind.ingest.selfcrawl import resolve_client

    fb = fallback_source_for(dataset_code)
    if fb is None or fb == source:
        return None
    try:
        return resolve_client(fb)
    except KeyError:
        return None


async def _fetch_with_fallback(
    upstream,
    *,
    dataset_code: str,
    symbol: str | None,
    range_start: date,
    range_end: date,
    source: str,
) -> tuple[list[dict[str, Any]], str]:
    """Primary fetch, with catalog-fallback routing on permanent 4xx.

    Returns `(rows, served_source)`: `served_source` is `source` when the
    primary answered, or the catalog's registered fallback source name
    when a fallback served the request instead. Callers need this to fix
    row provenance — `mapping.extra` (e.g. `{"source": "finmind"}`)
    stamps the PRIMARY source unconditionally, so `ingest_chunk` must
    overwrite the transformed rows' "source" field when a fallback
    actually served them.

    A fallback that itself raises (including NotImplementedError from a
    dataset the fallback client has no handler for) re-raises the
    ORIGINAL primary error — the operator should see the 422 that
    started it, not the fallback's stack.
    """
    try:
        rows = await upstream.fetch(dataset_code, symbol, range_start, range_end)
        return rows, source
    except Exception as primary_exc:
        if not _is_permanent_client_error(primary_exc):
            raise
        fallback = _resolve_fallback_client(dataset_code, source)
        if fallback is None:
            raise
        from finmind.dataset_catalog import fallback_source_for

        served_source = fallback_source_for(dataset_code) or source
        log.warning(
            "ingest_chunk: %s 4xx on %s — routing to catalog fallback %s",
            dataset_code, source, served_source,
        )
        try:
            rows = await fallback.fetch(
                dataset_code, symbol, range_start, range_end
            )
        except Exception as fallback_exc:
            log.warning(
                "ingest_chunk: %s fallback %s also failed: %s",
                dataset_code, served_source, redact_exception(fallback_exc),
            )
            raise primary_exc
        return rows, served_source


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
        safe_error = redact_exception(exc)
        chunk = await claim_chunk(
            session,
            dataset_code=dataset_code,
            symbol=symbol,
            source=source,
            range_start=range_start,
            range_end=range_end,
        )
        await skip_chunk(session, chunk.id, safe_error)
        await _update_dataset_telemetry(session, dataset_code, 0, safe_error)
        return ChunkResult("skipped", 0, safe_error)

    # 3. Claim chunk in 'running' state.
    chunk = await claim_chunk(
        session,
        dataset_code=dataset_code,
        symbol=symbol,
        source=source,
        range_start=range_start,
        range_end=range_end,
    )
    # Keep the scalar ID across a later rollback. SQLAlchemy expires ORM
    # objects on rollback even with expire_on_commit=False; reading chunk.id
    # afterward can otherwise trigger an async lazy-load outside greenlet.
    chunk_id = chunk.id

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
        raw_rows, served_source = await _fetch_with_fallback(
            upstream, dataset_code=dataset_code, symbol=symbol,
            range_start=range_start, range_end=range_end, source=source,
        )
        # Wide-format datasets (quarterly statements, market-wide
        # institutional totals) need to pivot N FinMind rows → 1
        # local row. When `batch_transform` is set on the mapping
        # the runner delegates the entire chunk to it; otherwise
        # the per-row `transform_row` path runs (the common case).
        if mapping.batch_transform is not None:
            transformed = mapping.batch_transform(raw_rows)
        else:
            transformed = [transform_row(r, mapping) for r in raw_rows]
        # Drop rows whose PK columns are NULL — corrupt upstream data
        # otherwise raises a constraint violation that aborts the
        # transaction and leaves the chunk stuck running.
        transformed = [
            r for r in transformed
            if all(r.get(pk) is not None for pk in mapping.pk_columns)
        ]
        if served_source != source:
            # `mapping.extra` (e.g. {"source": "finmind"}) stamps the
            # PRIMARY source unconditionally, before we know whether a
            # fallback ended up serving the chunk — correct the
            # provenance here so fallback-served rows don't claim
            # FinMind lineage they don't have.
            for r in transformed:
                if "source" in r:
                    r["source"] = served_source
    except Exception as exc:
        safe_error = redact_exception(exc)
        log.error(
            "ingest_chunk fetch/transform failed: %s %s: %s",
            dataset_code,
            symbol,
            safe_error,
        )
        await fail_chunk(session, chunk_id, safe_error)
        await _update_dataset_telemetry(session, dataset_code, 0, safe_error)
        return ChunkResult("failed", 0, safe_error)

    try:
        rows_written = await _upsert_rows(session, mapping, transformed)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        safe_error = redact_exception(exc)
        log.error(
            "ingest_chunk upsert failed: %s %s: %s",
            dataset_code,
            symbol,
            safe_error,
        )
        await fail_chunk(session, chunk_id, safe_error)
        await _update_dataset_telemetry(session, dataset_code, 0, safe_error)
        return ChunkResult("failed", 0, safe_error)

    await finish_chunk(session, chunk_id, rows_written)
    telemetry_error = (
        f"served_by_fallback:{served_source} (primary {source} failing)"
        if served_source != source else None
    )
    await _update_dataset_telemetry(
        session, dataset_code, rows_written, telemetry_error
    )
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
