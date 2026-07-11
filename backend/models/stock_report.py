"""B1 個股 AI 研究報告 archive (`stock_reports`).

One row per completed SSE report generation from
``POST /api/ai/stock-report/{market}/{symbol}``. The row is inserted
only after the LLM stream finishes successfully — a stream that dies
mid-way (or produces no content) persists nothing, so history never
contains half-reports.

`sections` is a best-effort ``{標題: 內文}`` parse of the markdown
``## `` headings (摘要/基本面/籌碼與資金/技術面/風險/結論) so future
features (B3 scoreboard 掛接, email digests) can address individual
sections without re-parsing `content_md`. NULL when the model ignored
the heading contract — `content_md` remains the source of truth.
"""
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class StockReport(Base):
    __tablename__ = "stock_reports"
    __table_args__ = (
        # User history list (`GET /api/ai/stock-reports`) — newest first.
        Index("ix_stock_reports_user_id_created_at", "user_id", "created_at"),
        # Per-symbol history (panel's "過往報告" list) — newest first.
        Index(
            "ix_stock_reports_symbol_market_created_at",
            "symbol", "market", "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default="",
    )
    sections: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]
