"""One row per chat completion. Token counts captured from the provider's
final usage chunk (where available); cost computed via a hardcoded rate table
in services/llm_usage_service.py.
"""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class LLMUsageEvent(Base):
    __tablename__ = "llm_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    persona_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True,
    )

    user: Mapped["User | None"] = relationship("User")  # type: ignore[name-defined]

    __table_args__ = (
        Index("ix_llm_usage_user_time", "user_id", "created_at"),
        Index("ix_llm_usage_provider_time", "provider", "created_at"),
    )
