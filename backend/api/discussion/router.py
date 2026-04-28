"""Discussion API.

A "discussion" is a multi-persona round-table where N AI experts argue a
user-supplied topic under user-supplied rules. The user controls when to
advance a round and when to call for a conclusion; LLM cost per round is
proportional to the persona roster size, so the analyst-role daily quota
is decremented `len(persona_ids)` times per round.

Endpoints:
  GET    /api/discussion/sessions               – list user's discussions
  POST   /api/discussion/sessions               – create new (draft state)
  GET    /api/discussion/sessions/{id}          – detail with all turns
  PATCH  /api/discussion/sessions/{id}          – edit topic/rules/personas (draft only)
  DELETE /api/discussion/sessions/{id}          – owner-only cascade delete
  POST   /api/discussion/sessions/{id}/round    – SSE: stream one round
  POST   /api/discussion/sessions/{id}/conclude – synthesize structured result

The SSE round endpoint emits the same envelope shape as `/api/ai/chat`
(`data: {...}\\n\\n` + `[DONE]` terminator) so the frontend can reuse the
existing parsing pattern.

Quota: each persona-turn counts as one AI request. Viewers get 5/day,
analysts/admins 20/day — same as the chat endpoint. The synthesizer is
also one request.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion.schemas import (
    ConclusionResponse,
    CreateDiscussionRequest,
    DiscussionDetailResponse,
    DiscussionResponse,
    TurnResponse,
    UpdateDiscussionRequest,
)
from auth.permissions import require_viewer
from cache.redis_cache import cache_decr, cache_incr, key_ai_counter
from config import settings
from db.session import get_db
from models.discussion import Discussion
from services import discussion_service

log = logging.getLogger(__name__)
router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]


# ── quota ──────────────────────────────────────────────────────────


def _daily_limit(role: str) -> int:
    if role in ("analyst", "admin"):
        return settings.AI_REQUESTS_ANALYST_DAILY
    return settings.AI_REQUESTS_VIEWER_DAILY


async def _check_quota(user: dict, *, cost: int) -> None:
    """Reserve `cost` requests against the daily counter atomically.

    Done as a single `INCRBY` so two concurrent rounds can't both squeak
    under the limit. If the post-increment count exceeds the cap we
    refund and reject.
    """
    limit = _daily_limit(user.get("role", "viewer"))
    new_count = 0
    for _ in range(cost):
        new_count = await cache_incr(key_ai_counter(user["id"]), ttl_seconds=86400)
    if new_count > limit:
        for _ in range(cost):
            await cache_decr(key_ai_counter(user["id"]))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily AI quota exceeded ({limit} requests/day). "
                "Resets at midnight UTC."
            ),
        )


async def _refund(user: dict, *, count: int) -> None:
    for _ in range(count):
        try:
            await cache_decr(key_ai_counter(user["id"]))
        except Exception as exc:
            log.error(
                "discussion.quota.refund_failed",
                extra={"user_id": user.get("id"), "error": str(exc)},
            )
            return


def _coerce_owner_uuid(user: dict) -> uuid.UUID:
    raw = user.get("id")
    if isinstance(raw, uuid.UUID):
        return raw
    return uuid.UUID(str(raw))


def _to_response(d: Discussion) -> DiscussionResponse:
    return DiscussionResponse(
        id=d.id,
        topic=d.topic,
        rules=d.rules,
        persona_ids=list(d.persona_ids or []),
        status=d.status,
        current_round=d.current_round,
        conclusion=d.conclusion,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


# ── routes ─────────────────────────────────────────────────────────


@router.get("/sessions", response_model=list[DiscussionResponse])
async def list_sessions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = await discussion_service.list_discussions(
        db, owner_id=_coerce_owner_uuid(user),
    )
    return [_to_response(r) for r in rows]


@router.post("/sessions", response_model=DiscussionResponse, status_code=201)
async def create_session(
    body: CreateDiscussionRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        row = await discussion_service.create_discussion(
            db,
            owner_id=_coerce_owner_uuid(user),
            topic=body.topic,
            rules=body.rules,
            persona_ids=body.persona_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _to_response(row)


@router.get("/sessions/{discussion_id}", response_model=DiscussionDetailResponse)
async def get_session(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    turns = await discussion_service.get_turns(db, discussion_id=row.id)
    base = _to_response(row).model_dump()
    base["turns"] = [
        TurnResponse(
            id=t.id,
            round=t.round,
            turn_index=t.turn_index,
            persona_id=t.persona_id,
            stance=t.stance,
            content=t.content,
            citations=t.citations,
            created_at=t.created_at,
        )
        for t in turns
    ]
    return DiscussionDetailResponse(**base)


@router.patch("/sessions/{discussion_id}", response_model=DiscussionResponse)
async def update_session(
    discussion_id: uuid.UUID,
    body: UpdateDiscussionRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    try:
        row = await discussion_service.update_discussion(
            db,
            row,
            topic=body.topic,
            rules=body.rules,
            persona_ids=body.persona_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _to_response(row)


@router.delete("/sessions/{discussion_id}", status_code=204)
async def delete_session(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    deleted = await discussion_service.delete_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Discussion not found")


@router.post("/sessions/{discussion_id}/round")
async def run_round(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Stream one round of the discussion as Server-Sent Events.

    Each persona-turn counts as one AI request. The full cost
    (`len(persona_ids)`) is reserved up front; if any persona's stream
    fails we keep what we charged because the partial round is still
    persisted.
    """
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if row.status == discussion_service.STATUS_RUNNING:
        raise HTTPException(
            status_code=409,
            detail="A round is already in progress for this discussion",
        )

    cost = len(row.persona_ids or [])
    await _check_quota(user, cost=cost)

    async def event_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for ev in discussion_service.run_round(
                db, row, user_id=str(user["id"]),
            ):
                payload = {"type": ev.type, **ev.payload}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
        except Exception as exc:
            log.exception("discussion.round.stream_failed",
                          extra={"discussion_id": str(discussion_id)})
            err = json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False)
            yield f"data: {err}\n\n".encode()
        finally:
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{discussion_id}/conclude", response_model=ConclusionResponse)
async def conclude_session(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if row.current_round == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot synthesize a conclusion before any round has run",
        )

    await _check_quota(user, cost=1)
    try:
        conclusion = await discussion_service.synthesize_conclusion(
            db, row, user_id=str(user["id"]),
        )
    except Exception:
        await _refund(user, count=1)
        raise
    return ConclusionResponse(discussion_id=row.id, conclusion=conclusion)
