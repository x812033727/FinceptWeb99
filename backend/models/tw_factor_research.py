"""Persisted TW factor research runs and user-scoped model registry."""
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwFactorResearchRun(Base):
    __tablename__ = "tw_factor_research_runs"
    __table_args__ = (
        Index("ix_tw_factor_research_user_created", "user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    profile: Mapped[str] = mapped_column(String(24), nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    gate_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
    )


class TwFactorModelVersion(Base):
    __tablename__ = "tw_factor_model_versions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "profile", "version_number",
            name="uq_tw_factor_model_user_profile_version",
        ),
        Index(
            "ix_tw_factor_model_user_profile_status",
            "user_id", "profile", "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    profile: Mapped[str] = mapped_column(String(24), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    weights: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    gate_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("tw_factor_research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promotion_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
    )
