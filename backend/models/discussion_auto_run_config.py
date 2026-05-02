"""ORM model for per-user daily auto-run discussion opt-in.

One row per user (PK = user_id). Owned by the user themselves so the
DiscussionPage's owner-scoped session list naturally surfaces the
generated auto-run discussions in their own sidebar — no separate
"public feed" endpoint needed.

`topic` and `rules` are NOT NULL on purpose: there is no compiled-in
default at the auto-run task layer any more, so the user must supply
both before flipping `enabled` to true. The service layer enforces
2 ≤ len(persona_ids) ≤ 8 to mirror the manual `/sessions` POST bounds.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DiscussionAutoRunConfig(Base):
    __tablename__ = "discussion_auto_run_configs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )
    persona_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    market: Mapped[str] = mapped_column(
        String(8), nullable=False, default="TW", server_default="TW",
    )
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
