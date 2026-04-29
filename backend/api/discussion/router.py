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

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
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
from db.session import get_db, get_db_session_factory
from models.discussion import Discussion
from services import discussion_service

log = logging.getLogger(__name__)
router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]

# Detached background tasks for in-flight rounds. Kept module-level so
# tasks aren't garbage-collected when the originating request returns
# its StreamingResponse — the SSE consumer might disconnect long before
# the round actually completes, and we want the task to live until
# `run_round` finishes persisting turns + status reset + refund.
_BG_ROUND_TASKS: dict[uuid.UUID, asyncio.Task] = {}


# ── quota ──────────────────────────────────────────────────────────


async def _daily_limit(db: AsyncSession, role: str) -> int:
    """Resolve the user's daily quota via runtime_config_service so an
    admin can retune AI_REQUESTS_VIEWER_DAILY / ANALYST_DAILY from the
    UI without redeploying. Falls back to the compiled default on any
    resolver failure."""
    key = "AI_REQUESTS_ANALYST_DAILY" if role in ("analyst", "admin") \
        else "AI_REQUESTS_VIEWER_DAILY"
    try:
        from services.runtime_config_service import get_int as _get_int
        return await _get_int(db, key)
    except Exception:
        return getattr(settings, key)


async def _check_quota(user: dict, db: AsyncSession, *, cost: int) -> None:
    """Reserve `cost` requests against the daily counter atomically.

    Done as a sequential INCR loop so two concurrent rounds can't both
    squeak under the limit (the final new_count check rejects whichever
    one crosses). If the post-increment count exceeds the cap we refund
    and reject.
    """
    limit = await _daily_limit(db, user.get("role", "viewer"))
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
        verdict=d.verdict,
        verdict_reason=d.verdict_reason,
        verified_at=d.verified_at,
        auto_run=d.auto_run,
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
    session_factory: Annotated[
        type[AsyncSession], Depends(get_db_session_factory)
    ],
):
    """Stream one round of the discussion as Server-Sent Events.

    Each persona-turn counts as one AI request. The full cost
    (`len(persona_ids)`) is reserved up front; we count `turn_end` events
    as we stream them and refund the unconsumed remainder if the round
    aborts early (LLM failure, persona timeout, client disconnect mid-
    stream). Persisted partial turns stay — only the unspent quota is
    returned.
    """
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if row.status == discussion_service.STATUS_RUNNING:
        # A previous round genuinely in progress would still be writing
        # turns / the SSE stream would still be open. If the row is
        # RUNNING but the most recent updated_at is far in the past,
        # the previous attempt died (process restart, client disconnect
        # before finally-block reset, status-reset commit failure) and
        # the discussion is permanently stuck. Auto-recover: force-
        # reset to DRAFT and let this request proceed.
        last_update = row.updated_at
        if last_update is not None and last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=UTC)
        stale = last_update is None or (
            datetime.now(UTC) - last_update
            > timedelta(seconds=settings.DISCUSSION_PERSONA_TIMEOUT_SECONDS * 2)
        )
        if stale:
            log.warning(
                "discussion.round.auto_recover_stale",
                extra={
                    "discussion_id": str(discussion_id),
                    "last_updated": str(row.updated_at),
                },
            )
            await discussion_service.force_reset_status(db, row)
        else:
            raise HTTPException(
                status_code=409,
                detail="A round is already in progress for this discussion",
            )

    cost = len(row.persona_ids or [])
    await _check_quota(user, db, cost=cost)

    # Run the round as a detached asyncio task with its own DB session so
    # the work survives client disconnect. The SSE response just observes
    # the task's progress via an in-memory queue. If the user closes
    # the browser mid-round, the SSE generator gets cancelled but the
    # background task continues to completion — turns persist, status
    # resets, quota refund fires.
    queue: asyncio.Queue = asyncio.Queue()
    owner_id = _coerce_owner_uuid(user)
    user_id_str = str(user["id"])

    async def _run_in_background() -> None:
        completed = 0
        try:
            async with session_factory() as bg_db:
                bg_row = await discussion_service.get_discussion(
                    bg_db,
                    discussion_id=discussion_id,
                    owner_id=owner_id,
                )
                if bg_row is None:
                    queue.put_nowait(
                        ("error", {"message": "Discussion not found"})
                    )
                    return
                try:
                    async for ev in discussion_service.run_round(
                        bg_db, bg_row, user_id=user_id_str,
                    ):
                        if ev.type == "turn_end":
                            completed += 1
                        queue.put_nowait((ev.type, ev.payload))
                except Exception as exc:
                    log.exception(
                        "discussion.round.background_failed",
                        extra={"discussion_id": str(discussion_id)},
                    )
                    queue.put_nowait(("error", {"message": str(exc)}))
        finally:
            # Refund happens in the background task so it fires whether
            # or not the SSE consumer is still attached. Persisted turns
            # keep their charge; only the unspent remainder is refunded.
            unconsumed = cost - completed
            if unconsumed > 0:
                try:
                    await _refund(user, count=unconsumed)
                except Exception:
                    log.exception(
                        "discussion.round.refund_failed",
                        extra={"discussion_id": str(discussion_id)},
                    )
            queue.put_nowait(None)

    task = asyncio.create_task(_run_in_background())
    # Keep a reference so the task isn't garbage-collected mid-run.
    # The done-callback removes the entry once the task settles.
    _BG_ROUND_TASKS[discussion_id] = task

    def _cleanup(_t: asyncio.Task) -> None:
        _BG_ROUND_TASKS.pop(discussion_id, None)

    task.add_done_callback(_cleanup)

    async def event_stream() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                etype, payload = item
                data = {"type": etype, **payload}
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except asyncio.CancelledError:
            log.info(
                "discussion.sse.client_disconnect_task_continues",
                extra={"discussion_id": str(discussion_id)},
            )
            raise

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

    await _check_quota(user, db, cost=1)
    try:
        conclusion = await discussion_service.synthesize_conclusion(
            db, row, user_id=str(user["id"]),
        )
    except Exception:
        await _refund(user, count=1)
        raise
    return ConclusionResponse(discussion_id=row.id, conclusion=conclusion)
