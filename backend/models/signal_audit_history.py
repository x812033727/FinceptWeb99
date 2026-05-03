"""ORM model for daily snapshots of signal-audit stats.

One row per (captured_at, market, signal). Populated by the daily
``snapshot_signal_audit`` cron, read by the AdminPage sparkline UI.

Reads pull a single signal's history within a date range, so the
secondary index is on (signal, market, captured_at).
"""
from datetime import date, datetime

from sqlalchemy import (
    Date, DateTime, Index, Integer, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class SignalAuditHistory(Base):
    __tablename__ = "signal_audit_history"
    __table_args__ = (
        Index(
            "ix_signal_audit_history_lookup",
            "signal", "market", "captured_at",
        ),
    )

    captured_at: Mapped[date] = mapped_column(Date, primary_key=True)
    # NULL = all-markets aggregate; otherwise scoped to TW / US / GLOBAL.
    # SQLite treats NULL in PK as distinct rows, which is fine for tests
    # — production Postgres declares this PK with `NULLS NOT DISTINCT`
    # via the migration so the upsert remains idempotent.
    market: Mapped[str | None] = mapped_column(
        String(12), primary_key=True, nullable=True,
    )
    signal: Mapped[str] = mapped_column(String(64), primary_key=True)

    discussions_audited: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    present_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    cited_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    cited_with_value_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    hallucinated_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    persona_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    persona_count_absent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
