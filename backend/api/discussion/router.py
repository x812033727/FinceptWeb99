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

R7/G8 pure-move split — this entry module now owns only the quota-
coupled round lifecycle (round SSE + inject/interject/conclude) plus the
sub-router wiring. The CRUD, per-round audit, auto-run config, and
post-mortem domains live in sibling modules (`sessions`, `contexts`,
`auto_run`, `post_mortem`) mounted below. `run_round`, `interject`, and
`conclude_session` stay here on purpose: they resolve `_refund` from this
module's namespace, and the test suite patches `api.discussion.router._refund`.
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

from api.discussion import auto_run as auto_run_router
from api.discussion import contexts as contexts_router
from api.discussion import lessons as lessons_router
from api.discussion import post_mortem as post_mortem_router
from api.discussion import sessions as sessions_router
from api.discussion import strategies as strategies_router
from api.discussion import sweeps as sweeps_router
from api.discussion._helpers import (  # noqa: F401  — re-exports kept for back-compat
    CurrentUser,
    _BG_ROUND_TASKS,
    _check_quota,
    _coerce_owner_uuid,
    _daily_limit,
    _refund,
    _sweep_to_response,
    _template_to_response,
    _to_response,
)
from api.discussion.schemas import (
    ConclusionResponse,
    InjectUserMessageRequest,
    InterjectRequest,
    InterjectResponse,
    TurnResponse,
)
from config import settings
from db.session import get_db, get_db_session_factory
from services import discussion_service
from services.discussion.round_runner.turn_exec import _MAX_TURN_ATTEMPTS
from services.discussion.symbol_names import enrich_conclusion_with_names

log = logging.getLogger(__name__)
router = APIRouter()
router.include_router(lessons_router.router)
router.include_router(sweeps_router.router)
router.include_router(strategies_router.router)
router.include_router(sessions_router.router)
router.include_router(contexts_router.router)
router.include_router(auto_run_router.router)
router.include_router(post_mortem_router.router)


# ── routes ─────────────────────────────────────────────────────────


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
        # Threshold = one worst-case TURN, not one worst-case round:
        # run_round bumps discussions.updated_at on every persisted
        # turn, so "no update for longer than a single turn could
        # possibly take (timeout × attempts) plus slack" means the
        # runner is dead, however many personas the round has. Read
        # the runtime-configurable timeout (DB override > env default)
        # — the compiled setting alone silently diverged from the
        # admin-tuned value.
        try:
            from services.runtime_config_service import get_int as _get_int
            persona_timeout = await _get_int(
                db, "DISCUSSION_PERSONA_TIMEOUT_SECONDS",
            )
        except Exception:
            persona_timeout = settings.DISCUSSION_PERSONA_TIMEOUT_SECONDS
        stale_after_s = persona_timeout * _MAX_TURN_ATTEMPTS * 2
        stale = last_update is None or (
            datetime.now(UTC) - last_update
            > timedelta(seconds=stale_after_s)
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
    user_role_str = str(user.get("role") or "")

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
                        bg_db, bg_row,
                        user_id=user_id_str,
                        user_role=user_role_str,
                    ):
                        # B4: interjected turns (the owner's question +
                        # the assigned persona's answer) are charged
                        # separately at /interject enqueue time — only
                        # roster turns consume this round's up-front
                        # reservation, so they must not inflate the
                        # `completed` counter the refund math uses.
                        if ev.type == "turn_end" and not ev.payload.get(
                            "injected_by_user"
                        ):
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


@router.post(
    "/sessions/{discussion_id}/inject",
    response_model=TurnResponse,
    status_code=201,
)
async def inject_user_message(
    discussion_id: uuid.UUID,
    body: InjectUserMessageRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Append a user-input turn to the current round so the next
    round's personas have to react to it.

    Owner-scoped. Requires the discussion to be in `draft` status
    (no in-flight round) and to have at least one round already
    completed — there's nothing to react to before round 1, and the
    user can edit topic/rules directly in that case. Does NOT
    consume AI quota — no LLM call.
    """
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    try:
        turn = await discussion_service.inject_user_message(
            db, row, content=body.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _turn_to_response(turn)


def _turn_to_response(turn) -> TurnResponse:
    return TurnResponse(
        id=turn.id,
        round=turn.round,
        turn_index=turn.turn_index,
        persona_id=turn.persona_id,
        stance=turn.stance,
        content=turn.content,
        citations=turn.citations,
        created_at=turn.created_at,
        injected_by_user=bool(getattr(turn, "injected_by_user", False)),
    )


@router.post(
    "/sessions/{discussion_id}/interject",
    response_model=InterjectResponse,
)
async def interject(
    discussion_id: uuid.UUID,
    body: InterjectRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """B4 圓桌討論插話 / 追問. Owner-scoped. Two modes:

    * Discussion RUNNING → the question is enqueued; `run_round`
      drains the queue at the next turn boundary, persists the
      question as a `user_input` turn and has the assigned persona
      (the named `target_persona`, else the moderator default = the
      first roster persona) answer it as an extra turn. Both turns
      stream over the round's existing SSE channel with
      `injected_by_user: true` and are persisted with the same flag.
      Returns `{status: "queued"}`.

    * Discussion CONCLUDED (`done` + conclusion) → 追問: one bounded
      follow-up turn runs synchronously (no new round, no context
      re-gather) and both turns are returned inline as
      `{status: "answered", question_turn, answer_turn}`.

    Any other state → 409 (between rounds the free-form
    `/sessions/{id}/inject` endpoint is the right tool — the personas
    react to it in the NEXT round).

    Quota: one AI request per interjection (the answer turn) — charged
    up front in both modes.
    """
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if body.target_persona and body.target_persona not in (row.persona_ids or []):
        raise HTTPException(
            status_code=400,
            detail=f"target_persona {body.target_persona!r} is not on this "
                   "discussion's roster",
        )

    if row.status == discussion_service.STATUS_RUNNING:
        # Capacity check BEFORE charging quota so a rejected request
        # never costs the user anything.
        if (
            discussion_service.pending_interjection_count(row.id)
            >= discussion_service._MAX_PENDING_INTERJECTIONS
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many pending interjections — wait for the "
                       "current ones to be answered",
            )
        await _check_quota(user, db, cost=1)
        try:
            discussion_service.queue_interjection(
                row.id,
                question=body.question,
                target_persona=body.target_persona,
            )
        except ValueError as exc:
            await _refund(user, count=1)
            raise HTTPException(status_code=400, detail=str(exc))
        return InterjectResponse(
            status="queued", target_persona=body.target_persona,
        )

    if row.status == discussion_service.STATUS_DONE and row.conclusion:
        await _check_quota(user, db, cost=1)
        try:
            question_turn, answer_turn = await discussion_service.interject_followup(
                db, row,
                question=body.question,
                target_persona=body.target_persona,
                user_id=str(user["id"]),
                user_role=str(user.get("role") or ""),
            )
        except ValueError as exc:
            await _refund(user, count=1)
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            await _refund(user, count=1)
            log.exception(
                "discussion.interject.followup_failed",
                extra={"discussion_id": str(discussion_id)},
            )
            raise HTTPException(
                status_code=502,
                detail=f"Follow-up failed: {exc}",
            )
        return InterjectResponse(
            status="answered",
            target_persona=answer_turn.persona_id,
            question_turn=_turn_to_response(question_turn),
            answer_turn=_turn_to_response(answer_turn),
        )

    raise HTTPException(
        status_code=409,
        detail="Interject requires a running discussion (mid-round) or a "
               "concluded one (follow-up). Between rounds, use "
               "/sessions/{id}/inject instead.",
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
    enrich_conclusion_with_names(row.market, conclusion)
    return ConclusionResponse(discussion_id=row.id, conclusion=conclusion)
