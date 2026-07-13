from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db

from ..schemas import PostMortemGapRow, PostMortemGapsOut

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Post-mortem data-gap analysis (PR #262) ──────────────────────


_POST_MORTEM_GAPS_RECENT_MAX = 200


@router.get("/post-mortem-gaps", response_model=PostMortemGapsOut)
async def post_mortem_gaps(
    _: AdminUser, db: DB,
    recent: int = 30,
    market: str | None = None,
) -> PostMortemGapsOut:
    """Aggregate "missing data" mentions extracted from post-mortem
    persona reflections (PR #249 self-critique flow). Each persona
    answer to "what data is missing?" is keyword-matched against a
    curated taxonomy of data categories the platform doesn't yet
    provide; the roll-up surfaces the most-requested gaps as a
    prioritized backlog.

    `recent` is bounded — same blast-radius reasoning as the signal
    audit endpoint. `market` filters concluded discussions when set.

    Empty `gaps` list means either no concluded discussions in the
    window had post-mortem rounds, OR they did but personas didn't
    mention any taxonomy category. Both states are honest signals
    for the operator.
    """
    from services.post_mortem_analysis_service import (
        analyze_recent_post_mortems,
    )

    if recent < 1 or recent > _POST_MORTEM_GAPS_RECENT_MAX:
        raise HTTPException(
            400,
            f"recent must be in [1, {_POST_MORTEM_GAPS_RECENT_MAX}]; "
            f"got {recent}",
        )

    summary = await analyze_recent_post_mortems(
        db, limit=recent, market=market,
    )
    return PostMortemGapsOut(
        discussions_audited=summary.discussions_audited,
        discussion_ids=summary.discussion_ids,
        gaps=[
            PostMortemGapRow(
                category=g.category,
                mentions=g.mentions,
                discussions_mentioning=g.discussions_mentioning,
                persona_count_mentioning=g.persona_count_mentioning,
            )
            for g in summary.gaps
        ],
    )
