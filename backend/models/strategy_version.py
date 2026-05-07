"""Versioned history of a strategy's learned artifacts (PR-4a).

Every fit attempt of a strategy's persona weights or calibration
curve writes a new row here, with a per-strategy auto-incrementing
`version_number` and a `status` enum telling you whether it's the
currently-live version, superseded by a newer fit, or rolled back
manually.

Why a separate table:
  - the live `discussion_strategy_templates.persona_weights` /
    `calibration_curve` columns get overwritten in place by every
    fit (the legacy contract); without history there's no "what
    did this look like before" diagnostic and no rollback target.
  - keeps the read path for the live curve cheap (still a single
    column read on the template) while making history a separate,
    larger query that operators only run during diagnostics.

Status state machine (linear; no rejoin):
  active → superseded   (next fit landed cleanly)
  active → rolled_back  (operator promoted an older version back)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON, UUID as SqlUUID, DateTime, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_id", "artifact_kind", "version_number",
            name="uq_strategy_versions_strategy_kind_version",
        ),
        Index(
            "ix_strategy_versions_strategy_kind_status",
            "strategy_id", "artifact_kind", "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True), primary_key=True, default=uuid4,
    )
    strategy_id: Mapped[UUID] = mapped_column(
        SqlUUID(as_uuid=True),
        ForeignKey("discussion_strategy_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Per-strategy auto-incrementing — service computes via a
    # max(version_number) + 1 query under transaction so concurrent
    # fits race-safely. Starts at 1.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'weights' | 'calibration_curve'. Stored as plain string rather
    # than enum so adding a third artifact (e.g. prompt template
    # version) is a one-line service change rather than a migration.
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # The actual weights dict / curve list at fit time. Schema-free
    # so artifact_kind variants don't collide.
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    # How many training samples backed this fit. Lets the operator
    # see "this version was fit on 35 samples vs the next on 90"
    # when comparing rollback candidates.
    sample_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    # The sweep whose Phase 3 triggered the fit. NULL for manual
    # fits (auto_deploy=False operator workflow) and walk-forward
    # train-fold fits where the parent isn't a single sweep_id.
    source_sweep_id: Mapped[UUID | None] = mapped_column(
        SqlUUID(as_uuid=True),
        ForeignKey("backtest_sweeps.id", ondelete="SET NULL"),
        nullable=True,
    )
    fit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )
    # 'active' = currently serving predictions; 'superseded' = an
    # older fit that's been replaced by a newer one; 'rolled_back'
    # = an operator manually promoted an older version, marking
    # this one as deprecated. Plain string for the same reason as
    # artifact_kind.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active",
        server_default="active",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
