import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class StockPickRun(Base):
    """One immutable daily ranking derived from traceable AI reports."""

    __tablename__ = "stock_pick_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "market", "run_date", name="uq_stock_pick_run_user_market_date"),
        Index("ix_stock_pick_runs_user_generated", "user_id", "generated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    methodology_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="trusted-report-ranking-v1",
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_report_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
    )
