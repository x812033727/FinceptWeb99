"""Shared router, auth deps, and DB-reachability probe for the FinMind
admin proxy sub-modules.

Every sub-module (`datasets`, `config_status`, `plans`, `keys`) imports
the same `router` object from here and hangs its endpoints off it, so the
final mounted route set is identical to the pre-split single-file module.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from finmind.db.session import get_finmind_db
from models.user import User

log = logging.getLogger("api.admin.finmind_proxy")

router = APIRouter()


AdminUser = Annotated[User, Depends(require_admin)]
FmDb = Annotated[AsyncSession, Depends(get_finmind_db)]

async def _ensure_finmind_db_reachable(db: AsyncSession) -> None:
    """Probe the FinMind clone DB and raise 503 on failure.

    Read endpoints that the AdminPage auto-fires on mount call this
    first so a fresh deployment (postgres_finmind not yet up) gets a
    clean 503 banner instead of a generic 500. The Setup checklist
    card on the same page renders the actionable fix hint, so the
    response detail just names the underlying exception class and
    points down to it.

    Mutating endpoints (PATCH/POST/DELETE) deliberately skip this
    probe — those are explicit operator actions where a generic
    failure already reads as "your click didn't work" and the inline
    error message surfaces the real cause via try/except in the
    handler body."""
    from sqlalchemy import text as _text

    from finmind.config import finmind_settings

    try:
        await db.execute(_text("SELECT 1"))
    except Exception as exc:
        # Surface the URL we're failing to reach (password masked) so
        # the operator can immediately tell which mode is active —
        # port 5433 = Path A1 (postgres_finmind container), main host =
        # Path A2 (FINMIND_USE_MAIN_DB=true). Without this, the bare
        # exception class doesn't tell you whether the env var took
        # effect or not.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"FinMind clone DB unreachable ({exc.__class__.__name__}) "
                f"at {finmind_settings.effective_database_url_safe}"
                + (
                    f" (schema={finmind_settings.schema})"
                    if finmind_settings.schema else ""
                )
                + ". See the Setup checklist below for the fix."
            ),
        ) from exc
