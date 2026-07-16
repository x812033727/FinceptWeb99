"""Market-data provider API keys, encrypted at rest.

Parallel to LLMProviderKey but for market connectors (Finnhub today,
Polygon / FRED / FinMind tomorrow). One row per provider; admin-only
writes; the encrypted blob is decrypted on read by
`services/market_key_service.py` using a Fernet key derived from
JWT_SECRET_KEY.

The connector layer reads via `market_key_service.resolve_key(provider)`
which falls back to the `.env`-supplied `settings.<PROVIDER>_API_KEY`
when no DB row exists. This means deployments that already configured
Finnhub via env keep working with no migration step on the operator's
side; the admin UI is purely additive.
"""
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from models.user import User  # noqa: F401  -- register with mapper registry; otherwise connector-path imports raise InvalidRequestError when resolving the `updated_by` relationship.

if TYPE_CHECKING:
    pass


class MarketProviderKey(Base):
    __tablename__ = "market_provider_keys"

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
