"""ORM model for admin LLM provider/model overrides on background tasks.

Empty row → use the task's compiled-in default. Keyed by an internal
`task_id` constant defined in `services/system_task_config_service.py`;
unknown task IDs are rejected at the service layer so the admin UI can
list a fixed roster (no free-form input).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User


class SystemTaskConfig(Base):
    __tablename__ = "system_task_configs"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    updated_by: Mapped["User | None"] = relationship("User")  # type: ignore[name-defined]
