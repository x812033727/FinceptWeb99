import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DecisionJournalEntry(Base):
    __tablename__ = "decision_journal_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "source_type", "source_id", "symbol", name="uq_decision_journal_source_symbol"),
        Index("ix_decision_journal_user_prediction", "user_id", "prediction_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    prediction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False, default="long")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcomes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    transaction_cost_bps: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
