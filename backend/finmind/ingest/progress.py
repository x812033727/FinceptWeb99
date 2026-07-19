"""`backfill_progress` CRUD helpers — chunk lifecycle.

The runner calls these in order:

  start_chunk(...)   -> claim chunk, status='running', started_at=now
  finish_chunk(...)  -> status='done', finished_at=now, rows_written=N
  fail_chunk(...)    -> status='failed', finished_at=now, error_message=...
  skip_chunk(...)    -> status='skipped' (e.g. mapping not found)

`needs_backfill` is the resume entry point: pass a (dataset, source,
range) and it returns the rows still pending — the runner walks those
in order, skipping anything already done in a previous session.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.models.backfill_progress import BackfillProgress
from finmind.redaction import redact_secret_text


async def claim_chunk(
    session: AsyncSession,
    *,
    dataset_code: str,
    symbol: str | None,
    source: str,
    range_start: date,
    range_end: date,
) -> BackfillProgress:
    """Idempotently insert-or-update a chunk into status='running'.

    Idempotent because the runner may be restarted mid-flight — the
    UNIQUE (dataset_code, symbol, source, range_start, range_end)
    constraint plus ON CONFLICT DO UPDATE means re-claiming is safe
    and resets `started_at` so the stuck-running detector is fresh.
    """
    now = datetime.now(tz=timezone.utc)
    payload = {
        "dataset_code": dataset_code,
        "symbol": symbol,
        "source": source,
        "range_start": range_start,
        "range_end": range_end,
        "status": "running",
        "started_at": now,
        "finished_at": None,
        "rows_written": None,
        "error_message": None,
    }

    dialect = session.bind.dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(BackfillProgress).values(**payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            "dataset_code", "symbol", "source",
            "range_start", "range_end",
        ],
        set_={
            "status": "running",
            "started_at": now,
            "finished_at": None,
            "rows_written": None,
            "error_message": None,
        },
    )
    await session.execute(stmt)
    await session.commit()

    row = (
        await session.execute(
            select(BackfillProgress).where(
                BackfillProgress.dataset_code == dataset_code,
                BackfillProgress.symbol.is_(symbol) if symbol is None
                else BackfillProgress.symbol == symbol,
                BackfillProgress.source == source,
                BackfillProgress.range_start == range_start,
                BackfillProgress.range_end == range_end,
            )
        )
    ).scalar_one()
    return row


async def _set_chunk_status(
    session: AsyncSession,
    chunk_id: int,
    *,
    status: str,
    rows_written: int | None = None,
    error_message: str | None = None,
) -> None:
    """UPDATE via Core statement rather than ORM attribute mutation.

    Pure SQL avoids the `MissingGreenlet` that ORM lazy-loads can
    throw after a rollback in the runner's exception path — at that
    point the previously-fetched ORM object's attributes are expired
    and accessing them re-triggers IO outside the safe async window.
    """
    from sqlalchemy import update

    safe_error = redact_secret_text(error_message)
    await session.execute(
        update(BackfillProgress)
        .where(BackfillProgress.id == chunk_id)
        .values(
            status=status,
            rows_written=rows_written,
            error_message=(safe_error[:1000] if safe_error else None),
            finished_at=datetime.now(tz=timezone.utc),
        )
    )
    await session.commit()


async def finish_chunk(
    session: AsyncSession, chunk_id: int, rows_written: int
) -> None:
    await _set_chunk_status(
        session, chunk_id, status="done", rows_written=rows_written
    )


async def fail_chunk(
    session: AsyncSession, chunk_id: int, error: str
) -> None:
    await _set_chunk_status(
        session, chunk_id, status="failed", error_message=error
    )


async def skip_chunk(
    session: AsyncSession, chunk_id: int, reason: str
) -> None:
    await _set_chunk_status(
        session, chunk_id, status="skipped", error_message=reason
    )


async def is_done(
    session: AsyncSession,
    *,
    dataset_code: str,
    symbol: str | None,
    source: str,
    range_start: date,
    range_end: date,
) -> bool:
    """Resume helper — returns True iff this chunk has already been
    completed in a prior run. The runner can use this to skip whole
    chunks without going through the FinMind quota."""
    row = (
        await session.execute(
            select(BackfillProgress.status).where(
                BackfillProgress.dataset_code == dataset_code,
                BackfillProgress.symbol.is_(None) if symbol is None
                else BackfillProgress.symbol == symbol,
                BackfillProgress.source == source,
                BackfillProgress.range_start == range_start,
                BackfillProgress.range_end == range_end,
            )
        )
    ).scalar_one_or_none()
    return row == "done"
