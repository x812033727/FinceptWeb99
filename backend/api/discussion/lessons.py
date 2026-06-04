"""Discussion lessons sub-router.

Owner-scoped CRUD over `discussion_lessons` rows + the library /
archive surfaces used by `LessonLibraryPage`.

Mounted under `/api/discussion` so paths read like
`/api/discussion/lessons/...` — identical to before the split.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import CurrentUser, _coerce_owner_uuid
from db.session import get_db

router = APIRouter()


@router.get("/lessons")
async def list_lessons(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    market: str | None = None,
    limit: int = 50,
):
    """Return the caller's most-recent post-mortem lessons. Owner-
    scoped — admin gets their own bucket only. Drives the
    DiscussionLessonsCard in AdminPage and the per-discussion
    "本次學到的事" collapsible."""
    from services import discussion_lesson_service as svc
    rows = await svc.list_recent_lessons(
        db,
        owner_user_id=_coerce_owner_uuid(user),
        market=market,
        limit=limit,
    )
    return [svc.lesson_to_dict(r) for r in rows]


@router.delete("/lessons/{lesson_id}", status_code=204)
async def delete_lesson(
    lesson_id: int,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Owner-scoped hard delete. 404 when the row doesn't exist
    or belongs to another user — silent rejection prevents
    enumeration via the timing channel."""
    from services import discussion_lesson_service as svc
    deleted = await svc.delete_lesson(
        db, lesson_id=lesson_id,
        owner_user_id=_coerce_owner_uuid(user),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="lesson not found")


@router.get("/lessons/library")
async def get_lesson_library(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    market: str | None = None,
    tier: str | None = None,
    archived: bool = False,
    sort: str = "hit_rate",
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """PR-5b: filterable lesson browser for the LessonLibraryPage.
    Owner-scoped. Returns paginated results so the UI can drive
    next/prev without re-fetching the full set.

    `sort` ∈ {hit_rate, usage, recent, created}. Default
    `hit_rate` puts the highest-recent-rate lessons first
    (NULLs sorted last so newly-created unused rows don't pollute
    the top).
    """
    from services import discussion_lesson_service as svc
    payload = await svc.list_lessons_with_metrics(
        db,
        owner_user_id=_coerce_owner_uuid(user),
        market=market, tier=tier, archived=archived,
        sort=sort, search=search, limit=limit, offset=offset,
    )
    return payload


@router.get("/lessons/archived")
async def list_archived_lessons(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    market: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """PR-4c: admin surface for the soft-deleted lessons.
    Owner-scoped — operators only see their own learning archive's
    discards. Newest-archived first."""
    from sqlalchemy import select
    from models.discussion_lesson import DiscussionLesson
    from services.discussion_lesson_service import (
        lesson_to_dict,
        shared_owner_ids,
    )
    stmt = (
        select(DiscussionLesson)
        .where(
            DiscussionLesson.owner_user_id.in_(
                shared_owner_ids(_coerce_owner_uuid(user))
            ),
            DiscussionLesson.archived_at.is_not(None),
        )
        .order_by(DiscussionLesson.archived_at.desc())
        .limit(min(max(int(limit), 1), 200))
        .offset(max(int(offset), 0))
    )
    if market:
        stmt = stmt.where(DiscussionLesson.market == market)
    rows = list((await db.scalars(stmt)).all())
    return {
        "items": [lesson_to_dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@router.post(
    "/lessons/{lesson_id}/unarchive",
)
async def unarchive_lesson(
    lesson_id: int,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-4c: restore a previously soft-deleted lesson. Idempotent
    on already-active rows."""
    from sqlalchemy import select
    from models.discussion_lesson import DiscussionLesson
    from services.discussion_lesson_service import shared_owner_ids
    row = await db.scalar(
        select(DiscussionLesson).where(
            DiscussionLesson.id == lesson_id,
            DiscussionLesson.owner_user_id.in_(
                shared_owner_ids(_coerce_owner_uuid(user))
            ),
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    if row.archived_at is None:
        return {"unarchived": False, "lesson_id": lesson_id}
    row.archived_at = None
    await db.commit()
    return {"unarchived": True, "lesson_id": lesson_id}
