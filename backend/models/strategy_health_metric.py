"""Daily strategy health snapshot (PR-4b).

One row per (strategy_id, snapshot_date). The cron
`tasks.monitor_strategy_health` writes a row each day per active
strategy, computing rolling-30 brier (raw + calibrated), hit rate,
sample count, lesson hit rate, and `status_flags` describing any
detected drift / collapse signals.

The UI reads back N days of rows to render a sparkline trend per
strategy; any row with non-empty `status_flags` triggers an admin
notification via `notification_service`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON, UUID as SqlUUID, Date, DateTime, Float, ForeignKey,
    Index, Integer, PrimaryKeyConstraint, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class StrategyHealthMetric(Base):
    __tablename__ = "strategy_health_metrics"
    __table_args__ = (
        PrimaryKeyConstraint(
            "strategy_id", "snapshot_date",
            name="pk_strategy_health_metrics",
        ),
        Index(
            "ix_strategy_health_metrics_snapshot_date",
            "snapshot_date",
        ),
    )

    strategy_id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True),
        ForeignKey(
            "discussion_strategy_templates.id", ondelete="CASCADE",
        ),
        nullable=False,
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Rolling 30-day brier on raw confidence. NULL when fewer than
    # `MIN_SAMPLES_FOR_METRIC` resolved discussions exist in the window.
    brier_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Same window over calibrated confidence (PR-C2). NULL when no
    # discussion in the window had `calibrated_confidence` set
    # (cold-start strategies).
    calibrated_brier_30d: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    # win_count / total_count over the same window. NULL when the
    # window has no graded discussions.
    hit_rate_30d: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    sample_count_30d: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    # Lesson hit rate from `discussion_lessons.hit_count` /
    # `usage_count` over the strategy's owner. NULL when no lesson
    # has been used yet.
    lesson_hit_rate_30d: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    maturity_tier_at_snapshot: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    # JSONB array of strings: `["brier_drift", "low_sample"]` etc.
    # Empty array = clean snapshot. The cron computes flags from the
    # current row vs previous rows in the same series, so a
    # 30-day-old strategy with no signals is a perfectly valid
    # zero-flag row.
    status_flags: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
