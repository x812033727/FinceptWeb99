"""ORM model for reusable backtest-sweep strategy templates (PR-A).

A row captures the operator's sweep recipe: topic, rules, persona
roster, market, plus the runtime defaults the sweep launcher
should pre-fill. PR-B reads `id` to aggregate sweeps that share a
template; PR-C writes back `persona_weights` so the synthesizer
can up-weight personas that historically called dates correctly.

Soft-deleted via `deleted_at` so historical sweeps can still
resolve `template.name` for the aggregate dashboard.
"""
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON, UUID as SqlUUID, Boolean, DateTime, ForeignKey,
    Index, Integer, String, Text, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DiscussionStrategyTemplate(Base):
    __tablename__ = "discussion_strategy_templates"
    __table_args__ = (
        Index(
            "ix_strategy_templates_owner_active",
            "owner_id", "deleted_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True), primary_key=True, default=uuid4,
    )
    owner_id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    persona_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )

    default_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    default_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    default_auto_post_mortem: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )

    # PR-C: persona_id -> weight (float). Empty {} = uniform.
    persona_weights: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}",
    )
    weights_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # PR-C2: isotonic confidence-calibration curve fitted from the
    # rolling pool of (raw_confidence, outcome_binary) pairs across
    # this strategy's child sweeps. NULL until the pool reaches
    # MIN_SAMPLES_FOR_FIT (=30) outcomes; the synthesizer falls
    # back to raw confidence in the meantime.
    calibration_curve: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True,
    )
    calibration_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    calibration_sample_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    # PR-3: deployment gate for freshly-fitted calibration curves.
    # When a new fit fails any safety check (endpoint inversion,
    # huge per-point delta vs the live curve, recent brier
    # degradation), it lands here instead of overwriting
    # `calibration_curve`. Admin reviews + approves via API to
    # promote it. NULL whenever no review is queued. The live
    # curve keeps serving predictions until either a fresh fit
    # deploys cleanly or the pending one is approved.
    calibration_pending_curve: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True,
    )
    calibration_pending_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    calibration_pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # PR-4a: maturity tier — "where is this strategy in its
    # learning lifecycle". Computed by `strategy_maturity_service`
    # from sample count, sweep count, and recent-vs-baseline brier
    # signals. Five buckets:
    #   cold_start: zero production sweeps yet
    #   learning  : sweeps started but <30 calibration samples or <3 sweeps
    #   mature    : ≥30 samples + ≥3 sweeps + brier within ±15% of baseline
    #   drifting  : recent 30-sample brier ≥20% above historical baseline
    #   stale     : no production sweep in the last 30 days
    # The UI surfaces this as a colored badge on every strategy
    # card so the operator sees the lifecycle status at a glance
    # without drilling into raw metrics.
    maturity_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cold_start",
        server_default="cold_start",
    )
    maturity_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # PR-D: auto-schedule fields. Disabled by default — explicit
    # opt-in so a fresh template doesn't fire LLM cost on its own.
    auto_schedule_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    auto_schedule_cadence_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, default=24, server_default="24",
    )
    auto_schedule_anchor_offset_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=-1, server_default="-1",
    )
    auto_schedule_trading_days_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    auto_schedule_last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(), nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
