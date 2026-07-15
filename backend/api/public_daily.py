"""Anonymous, read-only projection of one publisher's latest daily result."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.agents import list_agents
from config import settings
from db.session import get_db
from limiter import limiter
from models.discussion import Discussion, DiscussionTurn
from models.user import User
from services.discussion.symbol_names import enrich_conclusion_with_names

router = APIRouter()

DISCLAIMER = (
    "本頁內容僅供資訊與研究參考，不構成投資建議、招攬或保證；投資有風險，"
    "請自行查證並依個人財務狀況審慎決策。"
)


class PublicTurn(BaseModel):
    round: int
    turn_index: int
    persona_id: str
    persona_name: str
    stance: str
    content: str


class PublicDailyResult(BaseModel):
    market: str
    topic: str
    created_at: str
    captured_session: dict[str, Any] | None
    conclusion: dict[str, Any]
    turns: list[PublicTurn]


class PublicDailyResponse(BaseModel):
    state: Literal["disabled", "empty", "ready"]
    result: PublicDailyResult | None = None
    disclaimer: str = DISCLAIMER


def _public_conclusion(value: dict[str, Any], market: str) -> dict[str, Any]:
    """Copy only fields intended for anonymous display (including nested fields)."""
    enriched = enrich_conclusion_with_names(market, dict(value)) or {}
    public: dict[str, Any] = {
        key: enriched[key]
        for key in ("recommended_symbols", "symbol_names", "reasoning", "risks", "time_horizon", "consensus_score")
        if key in enriched
    }
    recommendations = enriched.get("recommendations")
    if isinstance(recommendations, list):
        public["recommendations"] = [
            {key: item[key] for key in ("symbol", "confidence", "calibrated_confidence") if key in item}
            for item in recommendations if isinstance(item, dict)
        ]
    quality = enriched.get("quality_signals")
    if isinstance(quality, dict):
        public["quality_signals"] = {
            key: quality[key]
            for key in ("stance_distribution", "confidence_stats", "consensus_contradiction", "hallucination_warnings", "_skipped")
            if key in quality
        }
    return public


@router.get("/daily", response_model=PublicDailyResponse)
@limiter.limit("30/minute")
async def get_public_daily(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> PublicDailyResponse:
    # Shared caches may retain the result briefly. There is no account-scoped
    # data in this projection, and the publisher is fixed by deployment config.
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60, stale-while-revalidate=30"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    email = settings.PUBLIC_DAILY_RESULTS_OWNER_EMAIL.strip().lower()
    if not email:
        return PublicDailyResponse(state="disabled")

    owner_id = await db.scalar(
        select(User.id).where(func.lower(User.email) == email, User.is_active.is_(True))
    )
    if owner_id is None:
        return PublicDailyResponse(state="empty")

    rows = (await db.scalars(
        select(Discussion)
        .where(
            Discussion.owner_id == owner_id,
            Discussion.auto_run.is_(True),
            Discussion.status == "done",
            Discussion.conclusion.is_not(None),
        )
        .order_by(Discussion.created_at.desc())
    )).all()
    discussion = next(
        (
            row for row in rows
            if isinstance(row.conclusion, dict)
            and not row.conclusion.get("_parse_error")
            and isinstance(row.conclusion.get("reasoning"), str)
        ),
        None,
    )
    if discussion is None:
        return PublicDailyResponse(state="empty")

    turns = (await db.scalars(
        select(DiscussionTurn)
        .where(
            DiscussionTurn.discussion_id == discussion.id,
            DiscussionTurn.round.between(1, 5),
            DiscussionTurn.injected_by_user.is_(False),
            ~DiscussionTurn.persona_id.startswith("_system:"),
        )
        .order_by(DiscussionTurn.round, DiscussionTurn.turn_index)
    )).all()
    names = {item["id"]: item["name"] for item in list_agents()}
    conclusion = _public_conclusion(discussion.conclusion, discussion.market)
    captured = discussion.conclusion.get("captured_session")
    return PublicDailyResponse(
        state="ready",
        result=PublicDailyResult(
            market=discussion.market,
            topic=discussion.topic,
            created_at=discussion.created_at.isoformat(),
            captured_session=captured if isinstance(captured, dict) else None,
            conclusion=conclusion,
            turns=[
                PublicTurn(
                    round=turn.round,
                    turn_index=turn.turn_index,
                    persona_id=turn.persona_id,
                    persona_name=names.get(turn.persona_id, turn.persona_id),
                    stance=turn.stance,
                    content=turn.content,
                )
                for turn in turns
            ],
        ),
    )

