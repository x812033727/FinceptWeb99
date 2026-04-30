"""Per-user opt-in for the daily auto-run discussion task.

A row is created on first save (UI calls PUT). `enabled=False` rows
stay around so users can re-enable without retyping their topic /
rules / persona roster — the scheduler skips them on its scan.

Validation is delegated to `discussion_service` for persona-id /
topic / rules bounds so the auto-run config and the manual
/sessions POST share one source of truth — same min/max persona
count, same character caps. Topic and rules are required (no
fallback default at this layer or in the auto-run task).
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_auto_run_config import DiscussionAutoRunConfig
from services.discussion_service import (
    _MAX_RULES_CHARS,
    _MAX_TOPIC_CHARS,
    _normalize_persona_ids,
    _validate_text,
)

log = logging.getLogger(__name__)


async def get_config(
    db: AsyncSession, *, user_id: uuid.UUID,
) -> DiscussionAutoRunConfig | None:
    return await db.scalar(
        select(DiscussionAutoRunConfig).where(
            DiscussionAutoRunConfig.user_id == user_id,
        )
    )


async def upsert_config(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    enabled: bool,
    persona_ids: list[str],
    topic: str,
    rules: str,
) -> DiscussionAutoRunConfig:
    """Insert or update a user's auto-run config.

    Raises ValueError on validation failure (persona count out of
    bounds, unknown persona id, empty/oversized topic or rules).
    """
    pids = _normalize_persona_ids(persona_ids)
    topic = _validate_text(topic, field="topic", max_chars=_MAX_TOPIC_CHARS)
    rules = _validate_text(rules, field="rules", max_chars=_MAX_RULES_CHARS)

    row = await get_config(db, user_id=user_id)
    now = datetime.now(UTC)
    if row is None:
        row = DiscussionAutoRunConfig(
            user_id=user_id,
            enabled=bool(enabled),
            persona_ids=pids,
            topic=topic,
            rules=rules,
        )
        db.add(row)
    else:
        row.enabled = bool(enabled)
        row.persona_ids = pids
        row.topic = topic
        row.rules = rules
        row.updated_at = now
    await db.commit()
    await db.refresh(row)
    return row


async def list_enabled(db: AsyncSession) -> list[DiscussionAutoRunConfig]:
    """Return every config row whose `enabled` flag is true.

    Used by the daily scheduler. Order is stable (by user_id) so a
    flaky tick that processes the first half before crashing won't
    starve later users on a retry — but the per-user idempotency
    check at the task layer is the real correctness guarantee.
    """
    stmt = (
        select(DiscussionAutoRunConfig)
        .where(DiscussionAutoRunConfig.enabled.is_(True))
        .order_by(DiscussionAutoRunConfig.user_id)
    )
    return list((await db.scalars(stmt)).all())
