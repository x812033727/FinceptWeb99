import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class InvestmentThesis(Base):
    __tablename__ = "investment_theses"
    __table_args__ = (Index("ix_investment_theses_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    core_case: Mapped[str] = mapped_column(Text, nullable=False)
    catalysts: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    risks: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    valuation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    watch_conditions: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    review_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    events: Mapped[list["ThesisEvent"]] = relationship(back_populates="thesis", cascade="all, delete-orphan")


class ThesisEvent(Base):
    __tablename__ = "thesis_events"
    __table_args__ = (
        Index("ix_thesis_events_thesis_occurred", "thesis_id", "occurred_at"),
        UniqueConstraint("thesis_id", "event_type", "source_ref", name="uq_thesis_event_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thesis_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("investment_theses.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    thesis: Mapped[InvestmentThesis] = relationship(back_populates="events")
