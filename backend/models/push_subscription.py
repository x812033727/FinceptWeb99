import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class PushSubscription(Base):
    """One browser Web Push subscription (PR-D3 瀏覽器推播).

    A user holds one row per browser/device that opted in. `endpoint`
    is the push-service-minted URL and is globally unique, so the
    subscribe endpoint upserts by it — re-subscribing from the same
    browser (or a different account logging in there) re-binds the
    existing row instead of duplicating it.

    `keys` carries the client's `{p256dh, auth}` encryption keys as
    returned by `PushSubscription.toJSON()`. Delivery bookkeeping:
    `last_success_at` + `failed_count` (consecutive failures; the
    web_push transport deletes the row on HTTP 404/410 from the push
    service, or once failed_count reaches its prune threshold).
    """
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    keys: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )

    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]
