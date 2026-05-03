"""Owner-scoped context blocks (DB-bound).

  - `user_context` — the discussion owner's portfolio + watchlist
    summary plus overlap with `focus_symbols`. Personas like
    portfolio_advisor / risk_manager use it to give portfolio-aware
    advice instead of generic "should I buy 2330" answers.
  - `prior_discussions` — the owner's past concluded discussions
    whose topic / recommended_symbols overlap the current focus.
    Lets a persona reference "上次討論 2330 結論為加碼,實際 +5%"
    without burning a tool call.

Skipped entirely when `owner_id` is None (anonymous read paths). The
prior-discussions block additionally requires `focus_symbols`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

ErrorRecorder = Callable[[str, Exception], None]


async def fetch_user_context(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    owner_id: UUID,
    focus_symbols: list[str] | None,
    record_error: ErrorRecorder,
) -> None:
    try:
        from services.discussion_service import _assemble_user_context
        ctx["user_context"] = await _assemble_user_context(
            db, owner_id=owner_id, focus_symbols=focus_symbols,
        )
    except Exception as exc:
        record_error("user_context", exc)


async def fetch_prior_discussions(
    ctx: dict[str, Any],
    db: "AsyncSession",
    *,
    owner_id: UUID,
    focus_symbols: list[str],
    exclude_discussion_id: UUID | None,
    as_of_dt: datetime | None,
    record_error: ErrorRecorder,
) -> None:
    try:
        from services.discussion_service import _assemble_prior_discussions
        ctx["prior_discussions"] = await _assemble_prior_discussions(
            db,
            owner_id=owner_id,
            focus_symbols=focus_symbols,
            exclude_id=exclude_discussion_id,
            as_of=as_of_dt,
        )
    except Exception as exc:
        record_error("prior_discussions", exc)
