"""ORM model for automated multi-day backtest sweep jobs (PR #274).

One row = one operator-initiated sweep that fans out to N child
discussions (one per resolved trading day). The worker that
processes the sweep is a background asyncio task fired on /start;
the row's `status` field is the source of truth for whether
the worker is active.

Status state machine:
  pending → running → completed
                  ↘ cancelled
                  ↘ failed

`resolved_dates` is filled at /start time (the worker resolves
the actual trading days from `ohlcv_daily` once and records them
so a mid-sweep restart can resume from `completed_dates`).
"""
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON, UUID as SqlUUID, Boolean, Date, DateTime, ForeignKey,
    Index, Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class BacktestSweep(Base):
    __tablename__ = "backtest_sweeps"
    __table_args__ = (
        Index("ix_backtest_sweeps_owner_status", "owner_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True), primary_key=True, default=uuid4,
    )
    owner_id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
        server_default="pending",
    )

    anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    trading_days_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5",
    )
    rounds_per_discussion: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    # PR #275: auto-trigger the post-mortem self-critique after
    # each spawned discussion's conclude. Default True — by the
    # time you're sweeping multiple days you almost always want
    # the critique attached.
    auto_post_mortem: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )

    topic: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    persona_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )

    resolved_dates: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )
    completed_dates: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )
    failed_dates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(), nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
