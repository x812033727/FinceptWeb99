"""Shared private helpers for the ingest repository package.

Extracted verbatim from the original monolithic ``repository.py`` because
these helpers are used by more than one domain module.
"""
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

# asyncpg's wire protocol caps a single statement at 32767 bind parameters.
# Market-wide ingests (e.g. 八大行庫 ~13K rows × 6 cols, 股權分散 ~35K rows × 9
# cols) blow past that in one shot and surface as InterfaceError. `_chunked_upsert`
# batches the payload so any market-wide bulk insert stays under the wire cap
# without callers having to think about it.
_PG_PARAM_LIMIT = 32000  # leave headroom under the 32767 hard cap


async def _chunked_upsert(
    db: AsyncSession,
    *,
    model: type,
    payload: list[dict[str, Any]],
    index_elements: list[str],
    update_cols: tuple[str, ...],
) -> int:
    """Dialect-aware ON CONFLICT upsert chunked under asyncpg's bind-param cap.

    Single chunk for small payloads (behaviourally identical to the previous
    one-shot insert); split for large ones.
    """
    if not payload:
        return 0
    cols = len(payload[0])
    chunk_size = max(1, _PG_PARAM_LIMIT // cols)
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert
    for i in range(0, len(payload), chunk_size):
        batch = payload[i:i + chunk_size]
        stmt = insert_fn(model).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements,
            set_={k: getattr(stmt.excluded, k) for k in update_cols},
        )
        await db.execute(stmt)
    await db.commit()
    return len(payload)


def _row_to_dict(row: Any, *, fields: tuple[str, ...]) -> dict[str, Any]:
    """Coerce a dataclass / ORM row to a dict for bulk-insert."""
    return {f: getattr(row, f) for f in fields}
