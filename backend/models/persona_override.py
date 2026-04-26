"""Admin overrides for per-persona LLM provider/model.

Empty row → use the persona's compiled-in default (`AgentSpec.default_provider`
+ `default_model`). Lets operators retune model selection (e.g. send Buffett to
Claude Opus, Trading Coach to Ollama) without redeploying.
"""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class PersonaOverride(Base):
    __tablename__ = "persona_overrides"

    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    updated_by: Mapped["User | None"] = relationship("User")  # type: ignore[name-defined]
