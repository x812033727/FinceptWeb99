"""backfill_progress — resumable Phase A FinMind backfill ledger.

A 10-year, 84-dataset, ~2000-symbol backfill against FinMind's sponsor
1500-req/hr quota takes ~45 hours of continuous runtime. Network blips
or process restarts must NOT force a restart from scratch — the runner
chunks each (dataset, symbol) into manageable date ranges, records the
start/end of each chunk here, and on resume queries this table to skip
any chunk already in `status='done'`.

Schema choices:
  - UNIQUE(dataset_code, symbol, source, range_start, range_end) — same
    chunk requested twice from the same source is a single ledger row,
    not two; rerunning is idempotent.
  - `symbol IS NULL` for market-wide datasets (e.g. `TaiwanStockInfo`,
    `TaiwanBusinessIndicator`) — `WHERE symbol IS NOT DISTINCT FROM
    :symbol` (Postgres) handles both shapes uniformly.
  - `source` in the unique key so the same chunk can be backfilled
    twice from different sources (e.g. once from FinMind during Phase
    A, again from TWSE during Phase B verification) without colliding.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class BackfillProgress(Base):
    __tablename__ = "backfill_progress"

    # `with_variant(Integer, "sqlite")` is required because SQLite's ROWID
    # auto-increment only kicks in for `INTEGER PRIMARY KEY` — it ignores
    # `autoincrement=True` on a `BIGINT` column. Postgres still gets
    # `BIGSERIAL` (sequence-backed BIGINT) for production scale.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )

    dataset_code: Mapped[str] = mapped_column(String(64), nullable=False)

    # NULL for market-wide datasets (no per-symbol axis).
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # 'finmind' | 'twse' | 'tpex' | 'taifex' | 'mops' | 'tdcc'
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    range_start: Mapped[date] = mapped_column(Date, nullable=False)
    range_end: Mapped[date] = mapped_column(Date, nullable=False)

    # 'pending' | 'running' | 'done' | 'failed' | 'skipped'
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )

    rows_written: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "dataset_code",
            "symbol",
            "source",
            "range_start",
            "range_end",
            name="uq_backfill_progress_chunk",
        ),
        # AdminPage health view: "how many chunks remain per dataset?"
        Index(
            "ix_backfill_progress_dataset_status",
            "dataset_code",
            "status",
        ),
        # Resume lookup: "give me the next pending/failed chunk for this
        # source." Predicate index keeps it tiny — done chunks (the
        # majority once backfill is mature) aren't carried in the index.
        Index(
            "ix_backfill_progress_resume",
            "dataset_code",
            "source",
            "status",
            postgresql_where="status IN ('pending', 'failed')",
        ),
    )
