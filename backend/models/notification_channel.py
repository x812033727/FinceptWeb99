"""Per-user delivery preferences for optional notification transports."""
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_notification_channels_user_kind"),
        Index("ix_notification_channels_binding_token_hash", "binding_token_hash", unique=True),
        Index("ix_notification_channels_destination", "destination", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Never stores provider credentials. For LINE, destination is the
    # opaque Messaging API user id. Email delivery resolves the user's
    # current account email at send time so profile changes cannot stale.
    destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    binding_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    binding_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
