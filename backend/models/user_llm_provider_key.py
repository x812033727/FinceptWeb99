"""Per-user LLM provider keys, encrypted at rest.

A user's row takes precedence over the system-wide row in `llm_provider_keys`,
which in turn beats the .env fallback. Same Fernet encryption as the system
table; key derived from JWT_SECRET_KEY via auth/llm_key_crypto.py.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class UserLLMProviderKey(Base):
    __tablename__ = "user_llm_provider_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_key: Mapped[str] = mapped_column(Text, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_validation_ok: Mapped[bool | None] = mapped_column(nullable=True)
    last_validation_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC),
    )
