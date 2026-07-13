import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db

from ..schemas import LessonPromoteOut

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.post(
    "/lessons/{lesson_id}/promote",
    response_model=LessonPromoteOut,
)
async def promote_lesson_to_structural(
    lesson_id: int,
    admin: AdminUser,
    db: DB,
) -> LessonPromoteOut:
    """PR-B2 follow-up — admin manual promotion of a lesson into the
    `structural` tier so it stops decaying entirely.

    The automatic episodic→semantic promotion lives in
    `lesson_tier_service.promote_eligible_lessons` (driven by
    Phase 3 of the sweep worker via usage + hit-rate gates).
    structural is intentionally admin-only because the threshold
    of "this is permanent advice" is qualitative judgement the
    system can't reliably infer. Once promoted, the only way out
    is another admin call — `services.lesson_tier_service.
    promote_to_structural` is idempotent so re-flipping a row
    doesn't error.

    Owner-scoped: each admin operates on their OWN learning
    history, mirroring the rest of the lesson API. Cross-owner
    promote returns 404 (don't reveal foreign-owner row
    existence). Multi-admin deployments where one admin needs to
    promote another's lessons should use a future explicitly-
    cross-tenant endpoint (none today by design).

    Returns the post-promotion state so the AdminPage UI can
    confirm the tier flip stuck without a follow-up GET.
    """
    from services.lesson_tier_service import promote_to_structural
    row = await promote_to_structural(
        db, lesson_id=lesson_id, owner_id=uuid.UUID(admin["id"]),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"lesson {lesson_id} not found",
        )
    return LessonPromoteOut(
        id=row.id,
        market=row.market,
        category=row.category,
        tier=row.tier,
        usage_count=row.usage_count,
        hit_count=row.hit_count,
        promoted_at=row.promoted_at,
        lesson_text=row.lesson_text,
    )
