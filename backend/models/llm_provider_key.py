"""LLM provider API keys, encrypted at rest.

One row per provider (`openai`, `anthropic`, `gemini`, `minimax`, …). When a
provider has no row, the LLM router falls back to the `.env`-supplied key in
`settings`. Admin-only writes; the encrypted blob is decrypted on read by
`services/llm_key_service.py` using a Fernet key derived from JWT_SECRET_KEY.
"""
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class LLMProviderKey(Base):
    __tablename__ = "llm_provider_keys"

    # Use provider id as natural primary key — exactly one row per provider.
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validation_ok: Mapped[bool | None] = mapped_column(nullable=True)
    last_validation_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC),
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    updated_by: Mapped["User | None"] = relationship("User")  # type: ignore[name-defined]
