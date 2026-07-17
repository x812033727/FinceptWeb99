"""Anonymous, read-only projection of one publisher's latest daily result."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ai.agents import list_agents
from cache.redis_cache import cache_get_json, cache_set_json
from config import settings
from db.session import get_db
from limiter import limiter
from models.discussion import Discussion, DiscussionTurn
from models.user import User
from services.daily_scoreboard_service import build_scoreboard
from services.discussion.symbol_names import (
    enrich_conclusion_with_names,
    resolve_display_name,
)

router = APIRouter()

DISCLAIMER = (
    "本頁內容僅供資訊與研究參考，不構成投資建議、招攬或保證；投資有風險，"
    "請自行查證並依個人財務狀況審慎決策。"
)

# How many recent run dates the public page shows.
RECENT_DAYS = 7

# Calendar-day bound on the driving query, anchored on the newest
# discussion (not on today — the page keeps showing the last runs even
# after a long publishing gap): generously covers RECENT_DAYS workdays
# plus holidays while keeping the scan from growing with all-time
# history.
QUERY_WINDOW_DAYS = 45

# The page is anonymous and identical for every visitor, so the whole
# response is shared in Redis for a minute — one query storm per TTL
# instead of per visitor. (The Cache-Control s-maxage below has no
# shared cache in front of it in this deployment.)
_CACHE_KEY = "public:daily:v1"
_CACHE_TTL_SECONDS = 60


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
    strategy: str = "general"
    sequence: int = 1
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    # Full ranked pool (slim, capped) — carried on the sequence-1 row only.
    candidate_pool: list[dict[str, Any]] = Field(default_factory=list)
    verdict: str | None = None
    verdict_reason: str | None = None
    verified_at: str | None = None
    verify_after_date: str | None = None
    day1_open_prices: dict[str, float] | None = None
    day5_close_prices: dict[str, float] | None = None
    daily_close_prices: dict[str, list[float | None]] | None = None


class PublicDailyResponse(BaseModel):
    state: Literal["disabled", "empty", "ready"]
    result: PublicDailyResult | None = None
    strategies: dict[str, list[PublicDailyResult]] = Field(default_factory=dict)
    days: list[PublicDailyDay] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class PublicDailyDay(BaseModel):
    date: str
    strategies: dict[str, list[PublicDailyResult]] = Field(default_factory=dict)


def _public_conclusion(value: dict[str, Any], market: str) -> dict[str, Any]:
    """Copy only fields intended for anonymous display (including nested fields)."""
    enriched = enrich_conclusion_with_names(market, dict(value)) or {}
    public: dict[str, Any] = {
        key: enriched[key]
        for key in (
            "recommended_symbols",
            "symbol_names",
            "reasoning",
            "risks",
            "time_horizon",
            "consensus_score",
        )
        if key in enriched
    }
    recommendations = enriched.get("recommendations")
    if isinstance(recommendations, list):
        public["recommendations"] = [
            {
                key: item[key]
                for key in ("symbol", "confidence", "calibrated_confidence")
                if key in item
            }
            for item in recommendations
            if isinstance(item, dict)
        ]
    quality = enriched.get("quality_signals")
    if isinstance(quality, dict):
        public["quality_signals"] = {
            key: quality[key]
            for key in (
                "stance_distribution",
                "confidence_stats",
                "consensus_contradiction",
                "hallucination_warnings",
                "_skipped",
            )
            if key in quality
        }
    return public


class ScoreboardEntry(BaseModel):
    strategy: str
    samples: int
    wins: int
    losses: int
    big_wins: int
    big_losses: int
    unverifiable: int
    win_rate: float | None = None
    avg_return_pct: float | None = None
    pool_samples: int = 0
    avg_alpha_pct: float | None = None


class PublicScoreboardResponse(BaseModel):
    state: Literal["disabled", "empty", "ready"]
    entries: list[ScoreboardEntry] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


_SCOREBOARD_CACHE_KEY = "public:daily:scoreboard:v1"
_SCOREBOARD_CACHE_TTL_SECONDS = 300


@router.get("/daily/scoreboard", response_model=PublicScoreboardResponse)
@limiter.limit("30/minute")
async def get_public_daily_scoreboard(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> PublicScoreboardResponse:
    """Aggregate win rate / realized 5-day return / pick-vs-pool alpha
    per daily strategy — same anonymous projection rules as /daily."""
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"

    email = settings.PUBLIC_DAILY_RESULTS_OWNER_EMAIL.strip().lower()
    if not email:
        return PublicScoreboardResponse(state="disabled")

    cached = await cache_get_json(_SCOREBOARD_CACHE_KEY)
    if cached is not None:
        return PublicScoreboardResponse.model_validate(cached)

    owner_id = await db.scalar(
        select(User.id).where(func.lower(User.email) == email, User.is_active.is_(True))
    )
    if owner_id is None:
        payload = PublicScoreboardResponse(state="empty")
    else:
        entries = await build_scoreboard(db, owner_id)
        payload = PublicScoreboardResponse(
            state="ready" if entries else "empty",
            entries=[ScoreboardEntry(**entry) for entry in entries],
        )
    await cache_set_json(
        _SCOREBOARD_CACHE_KEY,
        payload.model_dump(mode="json"),
        _SCOREBOARD_CACHE_TTL_SECONDS,
    )
    return payload


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

    cached = await cache_get_json(_CACHE_KEY)
    if cached is not None:
        return PublicDailyResponse.model_validate(cached)

    payload = await _build_response(db, email)
    await cache_set_json(_CACHE_KEY, payload.model_dump(mode="json"), _CACHE_TTL_SECONDS)
    return payload


async def _build_response(db: AsyncSession, email: str) -> PublicDailyResponse:
    owner_id = await db.scalar(
        select(User.id).where(func.lower(User.email) == email, User.is_active.is_(True))
    )
    if owner_id is None:
        return PublicDailyResponse(state="empty")

    latest_created = await db.scalar(
        select(func.max(Discussion.created_at)).where(
            Discussion.owner_id == owner_id,
            Discussion.auto_run.is_(True),
            Discussion.status == "done",
            Discussion.conclusion.is_not(None),
        )
    )
    if latest_created is None:
        return PublicDailyResponse(state="empty")

    cutoff = latest_created - timedelta(days=QUERY_WINDOW_DAYS)
    rows = (
        await db.scalars(
            select(Discussion)
            .options(load_only(
                Discussion.id,
                Discussion.market,
                Discussion.topic,
                Discussion.created_at,
                Discussion.conclusion,
                Discussion.candidate_snapshot,
                Discussion.auto_run_strategy,
                Discussion.auto_run_sequence,
                Discussion.auto_run_date,
                Discussion.verdict,
                Discussion.verdict_reason,
                Discussion.verified_at,
                Discussion.verify_after_date,
                Discussion.day1_open_prices,
                Discussion.day5_close_prices,
                Discussion.daily_close_prices,
            ))
            .where(
                Discussion.owner_id == owner_id,
                Discussion.auto_run.is_(True),
                Discussion.status == "done",
                Discussion.conclusion.is_not(None),
                Discussion.created_at >= cutoff,
            )
            .order_by(Discussion.created_at.desc())
        )
    ).all()
    discussion = next(
        (
            row
            for row in rows
            if isinstance(row.conclusion, dict)
            and not row.conclusion.get("_parse_error")
            and isinstance(row.conclusion.get("reasoning"), str)
        ),
        None,
    )
    if discussion is None:
        return PublicDailyResponse(state="empty")

    valid_rows = [
        row
        for row in rows
        if isinstance(row.conclusion, dict)
        and not row.conclusion.get("_parse_error")
        and isinstance(row.conclusion.get("reasoning"), str)
    ]

    def run_date(row: Discussion):
        return row.auto_run_date or row.created_at.astimezone(ZoneInfo("Asia/Taipei")).date()

    recent_dates = sorted({run_date(row) for row in valid_rows}, reverse=True)[:RECENT_DAYS]

    # One batched turns query for every discussion the page shows —
    # previously each projected row issued its own round-trip.
    recent_set = set(recent_dates)
    projected_rows = [row for row in valid_rows if run_date(row) in recent_set]
    projected_ids = {row.id for row in projected_rows} | {discussion.id}
    turns_by_discussion: dict[Any, list[DiscussionTurn]] = defaultdict(list)
    all_turns = (
        await db.scalars(
            select(DiscussionTurn)
            .where(
                DiscussionTurn.discussion_id.in_(projected_ids),
                DiscussionTurn.round.between(1, 5),
                DiscussionTurn.injected_by_user.is_(False),
                ~DiscussionTurn.persona_id.startswith("_system:"),
            )
            .order_by(DiscussionTurn.round, DiscussionTurn.turn_index)
        )
    ).all()
    for turn in all_turns:
        turns_by_discussion[turn.discussion_id].append(turn)

    names = {item["id"]: item["name"] for item in list_agents()}

    def _with_company_names(items: Any, market: str) -> list[dict[str, Any]]:
        """Attach the 公司簡稱 to each candidate/pool item. Names come
        from the in-memory TW symbol map (O(1), no I/O); items whose
        symbol has no known name pass through unchanged."""
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("symbol"):
                name = resolve_display_name(market, str(item["symbol"]))
                if name:
                    item = {**item, "name": name}
            out.append(item)
        return out

    def project(row: Discussion) -> PublicDailyResult:
        row_turns = turns_by_discussion.get(row.id, [])
        captured = row.conclusion.get("captured_session")
        snapshot = row.candidate_snapshot if isinstance(row.candidate_snapshot, dict) else {}
        return PublicDailyResult(
            market=row.market,
            topic=row.topic,
            created_at=row.created_at.isoformat(),
            captured_session=captured if isinstance(captured, dict) else None,
            conclusion=_public_conclusion(row.conclusion, row.market),
            turns=[
                PublicTurn(
                    round=t.round,
                    turn_index=t.turn_index,
                    persona_id=t.persona_id,
                    persona_name=names.get(t.persona_id, t.persona_id),
                    stance=t.stance,
                    content=t.content,
                )
                for t in row_turns
            ],
            strategy=row.auto_run_strategy or "general",
            sequence=row.auto_run_sequence or 1,
            candidates=_with_company_names(
                snapshot.get("candidates", []), row.market,
            ),
            candidate_pool=_with_company_names(
                snapshot.get("pool", []), row.market,
            ),
            verdict=row.verdict,
            verdict_reason=row.verdict_reason,
            verified_at=row.verified_at.isoformat() if row.verified_at else None,
            verify_after_date=(
                row.verify_after_date.isoformat() if row.verify_after_date else None
            ),
            day1_open_prices=row.day1_open_prices,
            day5_close_prices=row.day5_close_prices,
            daily_close_prices=row.daily_close_prices,
        )

    # Historical rows may still carry retired keys (chip_momentum, ...);
    # the setdefault below keeps those groups visible in the payload.
    strategy_keys = ("general", "chip_quality", "price_signal")
    days: list[PublicDailyDay] = []
    for day in recent_dates:
        day_grouped: dict[str, list[PublicDailyResult]] = {key: [] for key in strategy_keys}
        day_rows = sorted(
            (row for row in valid_rows if run_date(row) == day),
            key=lambda row: ((row.auto_run_strategy or "general"), row.auto_run_sequence or 1),
        )
        for row in day_rows:
            day_grouped.setdefault(row.auto_run_strategy or "general", []).append(
                project(row)
            )
        days.append(PublicDailyDay(date=day.isoformat(), strategies=day_grouped))
    grouped = days[0].strategies if days else {key: [] for key in strategy_keys}

    # `result` predates the multi-day/multi-strategy shape and mirrors
    # the newest valid discussion — same projection, kept for older
    # clients.
    return PublicDailyResponse(
        state="ready",
        strategies=grouped,
        days=days,
        result=project(discussion),
    )
