"""ORM model for the per-call ingest outcome archive.

One row per `record_health` call. Reads aggregate by day to render
the IngestHealthCard's 7-day sparkline. Index `(job_id, recorded_at)`
covers the only query the API runs — "give me the last N days of
outcomes for this job."
"""
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class IngestHealthHistory(Base):
    __tablename__ = "ingest_health_history"
    __table_args__ = (
        Index(
            "ix_ingest_health_history_lookup",
            "job_id", "recorded_at",
        ),
    )

    # `with_variant` keeps the production schema as BIGINT so the row
    # counter doesn't roll over at 2 billion (TimescaleDB hypertable
    # candidates routinely cross that), while letting SQLite tests use
    # plain INTEGER PRIMARY KEY (the only path that triggers SQLite's
    # implicit ROWID autoincrement — `BIGINT PRIMARY KEY` does NOT).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True, autoincrement=True,
    )
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    # Mirrors `_classify_outcome`: ok | silent_deny | failed | skipped.
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
