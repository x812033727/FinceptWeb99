import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class AlertCondition(str, enum.Enum):
    above = "above"
    below = "below"


class PriceAlert(Base):
    """One alert rule (PR-D1 規則引擎).

    `condition_type` + `params` drive the evaluator registry in
    `services/alert_rules.py`. The legacy `condition` enum +
    `target_price` columns are kept for backward compat: they stay
    populated for `price_above` / `price_below` rules (existing rows
    were data-migrated above→price_above, below→price_below in 0065)
    and are NULL for the newer rule types.

    Firing semantics: `repeat=False` → fire once, flip `triggered`
    (original behavior). `repeat=True` → `triggered` never flips;
    re-fires are gated by `last_fired_at + cooldown_seconds`.
    """
    __tablename__ = "price_alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    condition: Mapped[AlertCondition | None] = mapped_column(
        Enum(AlertCondition), nullable=True,
    )
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    condition_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="price_above",
        server_default="price_above",
    )
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cooldown_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    repeat: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    triggered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]


class AlertEvent(Base):
    """Immutable fired-alert history row (PR-D5).

    One row per alert firing. `price_alerts.triggered` remains the
    dedupe flag for the live rule; this table is the append-only
    audit trail that backs `GET /api/alerts/history` and the daily
    email digest. `kind` distinguishes price alerts from strategy-
    health degradation alerts (PR-D4, kind='strategy_health').
    `alert_id` is nullable: strategy-health events have no backing
    price_alerts row, and deleting a price alert must not erase its
    history (ondelete SET NULL).
    """
    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_events_user_id_fired_at", "user_id", "fired_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("price_alerts.id", ondelete="SET NULL"),
        nullable=True,
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="price")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC),
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
