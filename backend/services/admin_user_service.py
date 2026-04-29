"""Resolve `settings.ADMIN_EMAIL` to a `User.id`.

Lifted into its own module because both `tasks/auto_run_discussion.py`
and `tasks/verify_discussion_outcome.py` need an "owner" UUID for
system-driven discussions, and threading the same lookup through each
task's `_do_run` would copy boilerplate. Result is cached in-process
because admin email rarely changes during a pod's lifetime — a stale
in-memory value heals on the next pod restart, which is acceptable
for a config that rotates ~never.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.user import User

log = logging.getLogger(__name__)

_cache: dict[str, uuid.UUID | None] = {}


async def get_admin_owner_id(db: AsyncSession) -> uuid.UUID | None:
    """Return the User.id whose email matches `settings.ADMIN_EMAIL`.

    Returns None when ADMIN_EMAIL is empty or no matching user row
    exists — callers (system tasks) should record health failure with
    a clear message so operators see the misconfiguration in the admin
    dashboard. Never raises; a transient DB outage falls through to
    None too, which the same fail-soft handling covers.
    """
    email = (settings.ADMIN_EMAIL or "").strip().lower()
    if not email:
        return None
    cached = _cache.get(email)
    if cached is not None:
        return cached
    try:
        user_id = await db.scalar(
            select(User.id).where(User.email == email)
        )
    except Exception as exc:
        log.warning(
            "admin_user.lookup_failed",
            extra={"email": email, "error": str(exc)},
        )
        return None
    if user_id is not None:
        _cache[email] = user_id
    return user_id


def _invalidate_cache() -> None:
    """Used by tests to reset the in-process lookup cache between cases."""
    _cache.clear()
