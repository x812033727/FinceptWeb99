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
    PersonaWeightLearnResponse,
    StrategyTemplateCreate,
    StrategyTemplateResponse,
    StrategyTemplateUpdate,
    SweepAggregateResponse,
    AutoRunConfigRequest,
    AutoRunConfigResponse,
    ConclusionResponse,
    CreateDiscussionRequest,
    DiscussionDetailResponse,
    DiscussionResponse,
    InjectUserMessageRequest,
    BacktestSweepCreate,
    BacktestSweepFailedDate,
    BacktestSweepResponse,
    PostMortemDailyGainersOut,
    PostMortemDayPerformanceOut,
    PostMortemGainerOut,
    PostMortemRecommendedPerformanceOut,
    PostMortemResponse,
    PostMortemVerdictOut,
    PostMortemWinnerOut,
    ScoreboardResponse,
    ScoreboardRow,
    WalkForwardFoldOut,
    WalkForwardPlanResponse,
    WalkForwardRequest,
    TurnResponse,
    UpdateDiscussionRequest,
)
from auth.permissions import require_viewer
from cache.redis_cache import cache_decr, cache_incr, key_ai_counter
from config import settings
from db.session import get_db, get_db_session_factory
from models.discussion import Discussion
from services import discussion_auto_run_config_service, discussion_service

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
        market=d.market,
        status=d.status,
        current_round=d.current_round,
        conclusion=d.conclusion,
        post_mortem_conclusion=d.post_mortem_conclusion,
        verdict=d.verdict,
        verdict_reason=d.verdict_reason,
        verified_at=d.verified_at,
        auto_run=d.auto_run,
        day1_open_prices=d.day1_open_prices,
        day5_close_prices=d.day5_close_prices,
        daily_close_prices=d.daily_close_prices,
        as_of_date=d.as_of_date.isoformat() if d.as_of_date else None,
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
    as_of_parsed = None
    if body.as_of_date:
        try:
            from datetime import date as _date
            as_of_parsed = _date.fromisoformat(body.as_of_date)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"as_of_date must be ISO YYYY-MM-DD; got {body.as_of_date!r}",
            )
    try:
        row = await discussion_service.create_discussion(
            db,
            owner_id=_coerce_owner_uuid(user),
            topic=body.topic,
            rules=body.rules,
            persona_ids=body.persona_ids,
            market=body.market,
            as_of_date=as_of_parsed,
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


@router.get("/sessions/{discussion_id}/contexts")
async def get_round_contexts(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Per-round context snapshots for replay/audit. Owner-scoped —
    same access rule as the rest of the discussion endpoints. Returns
    `[{round, context, captured_at}, ...]` ordered by round; an empty
    list when the discussion was created before context-snapshot
    persistence was wired in (PR #135) or the snapshot writes failed
    silently."""
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    rows = await discussion_service.get_round_contexts(
        db, discussion_id=row.id,
    )
    return [
        {
            "round":       r.round,
            "context":     r.context,
            "captured_at": r.captured_at,
        }
        for r in rows
    ]


@router.get(
    "/sessions/{discussion_id}/scoreboard",
    response_model=ScoreboardResponse,
)
async def get_scoreboard(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """D1-D5 daily close + change % vs day-1 open per recommended
    symbol. Owner-scoped. Reads the persisted `daily_close_prices`
    column when populated (filled in by the daily 09:30 UTC cron);
    falls back to an on-demand compute against `ohlcv_daily` when
    NULL so newly-concluded discussions show partial data
    immediately instead of waiting a day."""
    from services import discussion_scoreboard_service

    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if not row.conclusion:
        raise HTTPException(
            status_code=400,
            detail="Discussion has no conclusion to score yet",
        )

    payload = await discussion_scoreboard_service.compute_scoreboard(
        db, row,
    )
    return ScoreboardResponse(
        discussion_id=row.id,
        anchor_date=payload["anchor_date"],
        created_at_tw_date=payload["created_at_tw_date"],
        rows=[ScoreboardRow(**r) for r in payload["rows"]],
    )


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
            market=body.market,
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


# ── auto-run config ────────────────────────────────────────────────


@router.get("/auto-run/config", response_model=AutoRunConfigResponse)
async def get_auto_run_config(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Read the current user's daily auto-run config. Returns a row of
    sensible defaults (`enabled=false`, empty topic / rules / personas)
    if the user has never saved one — the UI can render an empty form
    without a 404 dance."""
    row = await discussion_auto_run_config_service.get_config(
        db, user_id=_coerce_owner_uuid(user),
    )
    if row is None:
        return AutoRunConfigResponse(
            enabled=False,
            persona_ids=[],
            topic="",
            rules="",
            market="TW",
            updated_at=None,
        )
    return AutoRunConfigResponse(
        enabled=row.enabled,
        persona_ids=list(row.persona_ids or []),
        topic=row.topic,
        rules=row.rules,
        market=row.market,
        updated_at=row.updated_at,
    )


@router.put("/auto-run/config", response_model=AutoRunConfigResponse)
async def put_auto_run_config(
    body: AutoRunConfigRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        row = await discussion_auto_run_config_service.upsert_config(
            db,
            user_id=_coerce_owner_uuid(user),
            enabled=body.enabled,
            persona_ids=body.persona_ids,
            topic=body.topic,
            rules=body.rules,
            market=body.market,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AutoRunConfigResponse(
        enabled=row.enabled,
        persona_ids=list(row.persona_ids or []),
        topic=row.topic,
        rules=row.rules,
        market=row.market,
        updated_at=row.updated_at,
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
    return TurnResponse(
        id=turn.id,
        round=turn.round,
        turn_index=turn.turn_index,
        persona_id=turn.persona_id,
        stance=turn.stance,
        content=turn.content,
        citations=turn.citations,
        created_at=turn.created_at,
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


@router.post(
    "/sessions/{discussion_id}/post-mortem",
    response_model=PostMortemResponse,
)
async def run_post_mortem(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Inject a self-critique prompt against the actual next-trading-day
    top gainers, so the next round of personas has to defend or revise
    their recommendation against ground truth.

    Constraints:
      - Discussion must be a backtest (`as_of_date` not null) — live
        discussions don't have ground truth available yet.
      - Discussion must have a conclusion already (otherwise there's
        nothing to critique).
      - Discussion must be in `draft` status (no in-flight round).

    Flow:
      1. Compute next trading day's top-N gainers from
         ``ohlcv_daily``.
      2. Format a structured Chinese-prose self-critique prompt
         enumerating the gainers + the four review questions (hit/miss,
         missed-stocks-and-why, false-positive signals, missing data).
      3. Inject as a `user_input` turn so the next round's personas
         see it in their `## 先前發言` history.

    Returns the top-gainers data + the injected turn id. Caller is
    expected to follow up with the standard `/round` SSE endpoint
    to actually run the personas through the critique, then
    `/conclude` to re-synthesize a final conclusion.

    No AI quota is consumed by this endpoint itself — the LLM cost
    happens in the subsequent `/round` call.
    """
    from services.post_mortem_service import build_post_mortem_message

    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if row.as_of_date is None:
        raise HTTPException(
            status_code=400,
            detail="post_mortem requires backtest mode (as_of_date is null)",
        )
    if not row.conclusion:
        raise HTTPException(
            status_code=400,
            detail="post_mortem requires an existing conclusion — "
                   "run /conclude first",
        )

    payload = await build_post_mortem_message(db, row)
    if not payload.trading_days:
        raise HTTPException(
            status_code=400,
            detail="No ohlcv_daily data found in the post-as_of trading "
                   "window — archive may not reach this date.",
        )

    verdict = payload.verdict
    verdict_out = PostMortemVerdictOut(
        status=verdict.status,
        threshold_pct=verdict.threshold_pct,
        window_days=verdict.window_days,
        winners=[
            PostMortemWinnerOut(
                symbol=w.symbol,
                peak_pct=w.peak_pct,
                peak_day=w.peak_day.isoformat(),
            )
            for w in verdict.winners
        ],
        best_pct=verdict.best_pct,
        reason=verdict.reason,
    ) if verdict is not None else None

    # Win-skip: recommendation already cleared the threshold so we
    # short-circuit the inject + new round entirely. UI surfaces a
    # success badge based on `status="skipped"` + `verdict.winners`.
    skipped = verdict is not None and verdict.status == "win"
    if skipped:
        try:
            from middleware.metrics import POST_MORTEM_SKIPPED_TOTAL
            POST_MORTEM_SKIPPED_TOTAL.labels(market=row.market).inc()
        except Exception:
            pass
        injected_turn_id: int | None = None
    else:
        if not payload.prompt_text:
            # `insufficient_data` with non-empty trading_days (rare:
            # window > 0 but no recommended symbols had bars) — bail
            # so we don't inject an empty user_input turn.
            raise HTTPException(
                status_code=400,
                detail=(
                    verdict.reason if verdict is not None
                    else "Cannot build post-mortem prompt"
                ),
            )
        try:
            turn = await discussion_service.inject_user_message(
                db, row, content=payload.prompt_text,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        injected_turn_id = turn.id
        try:
            from middleware.metrics import POST_MORTEM_RAN_TOTAL
            POST_MORTEM_RAN_TOTAL.labels(market=row.market).inc()
        except Exception:
            pass

    # PR #273: serialise the new D1-D5 shape. Back-compat aliases
    # `next_trading_day` + `top_gainers` populated from D1's
    # leaderboard so older clients reading the flat shape still
    # see "next-day gainers".
    first_day = payload.trading_days[0]
    d1_gainers_block = next(
        (b for b in payload.daily_top_gainers if b.trading_day == first_day),
        None,
    )
    d1_gainers = d1_gainers_block.gainers if d1_gainers_block else []

    return PostMortemResponse(
        trading_days=[d.isoformat() for d in payload.trading_days],
        recommended_performance=[
            PostMortemRecommendedPerformanceOut(
                symbol=r.symbol,
                base_close=r.base_close,
                days=[
                    PostMortemDayPerformanceOut(
                        trading_day=dp.trading_day.isoformat(),
                        close=dp.close,
                        change_pct=dp.change_pct,
                    )
                    for dp in r.days
                ],
            )
            for r in payload.recommended_performance
        ],
        daily_top_gainers=[
            PostMortemDailyGainersOut(
                trading_day=block.trading_day.isoformat(),
                gainers=[
                    PostMortemGainerOut(
                        symbol=g.symbol, change_pct=g.change_pct,
                        close=g.close, base_close=g.base_close,
                        trading_day=g.trading_day.isoformat(),
                    )
                    for g in block.gainers
                ],
            )
            for block in payload.daily_top_gainers
        ],
        next_trading_day=first_day.isoformat(),
        top_gainers=[
            PostMortemGainerOut(
                symbol=g.symbol, change_pct=g.change_pct,
                close=g.close, base_close=g.base_close,
                trading_day=g.trading_day.isoformat(),
            )
            for g in d1_gainers
        ],
        status="skipped" if skipped else "ran",
        verdict=verdict_out,
        injected_turn_id=injected_turn_id,
    )


# ── Lessons CRUD (learning loop, PR #learning-mechanism) ─────────


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


# ── Backtest sweep (PR #274) ─────────────────────────────────────


def _sweep_to_response(s) -> BacktestSweepResponse:
    return BacktestSweepResponse(
        id=s.id,
        status=s.status,
        topic=s.topic,
        rules=s.rules,
        market=s.market,
        persona_ids=list(s.persona_ids or []),
        anchor_date=s.anchor_date.isoformat(),
        trading_days_count=s.trading_days_count,
        rounds_per_discussion=s.rounds_per_discussion,
        concurrency=s.concurrency,
        auto_post_mortem=bool(s.auto_post_mortem),
        strategy_id=s.strategy_id,
        resolved_dates=list(s.resolved_dates or []),
        completed_dates=list(s.completed_dates or []),
        failed_dates=[
            BacktestSweepFailedDate(**fd)
            for fd in (s.failed_dates or [])
        ],
        error_message=s.error_message,
        created_at=s.created_at,
        started_at=s.started_at,
        completed_at=s.completed_at,
        cancelled_at=s.cancelled_at,
    )


def _template_to_response(t) -> StrategyTemplateResponse:
    return StrategyTemplateResponse(
        id=t.id,
        name=t.name,
        description=t.description,
        topic=t.topic,
        rules=t.rules,
        market=t.market,
        persona_ids=list(t.persona_ids or []),
        default_rounds=t.default_rounds,
        default_concurrency=t.default_concurrency,
        default_auto_post_mortem=bool(t.default_auto_post_mortem),
        persona_weights=dict(t.persona_weights or {}),
        weights_updated_at=t.weights_updated_at,
        auto_schedule_enabled=bool(t.auto_schedule_enabled),
        auto_schedule_cadence_hours=t.auto_schedule_cadence_hours,
        auto_schedule_anchor_offset_days=t.auto_schedule_anchor_offset_days,
        auto_schedule_trading_days_count=t.auto_schedule_trading_days_count,
        auto_schedule_last_run_at=t.auto_schedule_last_run_at,
        created_at=t.created_at,
        updated_at=t.updated_at,
        deleted_at=t.deleted_at,
    )


@router.get("/sweeps", response_model=list[BacktestSweepResponse])
async def list_sweeps(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Most-recent-first list of the caller's sweeps."""
    from services import backtest_sweep_service as svc
    rows = await svc.list_sweeps(
        db, owner_id=_coerce_owner_uuid(user),
    )
    return [_sweep_to_response(r) for r in rows]


@router.post(
    "/sweeps",
    response_model=BacktestSweepResponse,
    status_code=201,
)
async def create_sweep(
    body: BacktestSweepCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Create a sweep in `pending` state. Caller follows up with
    `/sweeps/{id}/start` to fire the background worker."""
    from datetime import date as _date
    from services import backtest_sweep_service as svc

    try:
        anchor = _date.fromisoformat(body.anchor_date)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"anchor_date must be ISO date (YYYY-MM-DD), got "
                   f"{body.anchor_date!r}",
        )

    # PR-A: when strategy_id is supplied, fill any unset caller field
    # from the template. Caller-supplied fields always win, so the UI
    # can override individual knobs without forking the template.
    topic = body.topic
    rules = body.rules
    market = body.market
    persona_ids = body.persona_ids
    rounds = body.rounds_per_discussion
    concurrency = body.concurrency
    auto_pm = body.auto_post_mortem

    if body.strategy_id is not None:
        from services import strategy_template_service as tsvc
        tmpl = await tsvc.get_template(
            db, template_id=body.strategy_id,
            owner_id=_coerce_owner_uuid(user),
        )
        if tmpl is None:
            raise HTTPException(
                status_code=404,
                detail="strategy_id not found or not owned by caller",
            )
        topic = topic or tmpl.topic
        rules = rules or tmpl.rules
        market = market or tmpl.market
        persona_ids = persona_ids or list(tmpl.persona_ids or [])
        rounds = rounds if rounds is not None else tmpl.default_rounds
        concurrency = (
            concurrency if concurrency is not None
            else tmpl.default_concurrency
        )
        auto_pm = (
            auto_pm if auto_pm is not None
            else tmpl.default_auto_post_mortem
        )

    if not topic or not rules or not market or not persona_ids:
        raise HTTPException(
            status_code=400,
            detail="topic, rules, market and persona_ids are required "
                   "(provide them inline or via strategy_id)",
        )

    try:
        sweep = await svc.create_sweep(
            db, owner_id=_coerce_owner_uuid(user),
            topic=topic, rules=rules,
            market=market, persona_ids=persona_ids,
            anchor_date=anchor,
            trading_days_count=body.trading_days_count,
            rounds_per_discussion=rounds if rounds is not None else 1,
            concurrency=concurrency if concurrency is not None else 1,
            auto_post_mortem=auto_pm if auto_pm is not None else True,
            strategy_id=body.strategy_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _sweep_to_response(sweep)


@router.get(
    "/sweeps/{sweep_id}",
    response_model=BacktestSweepResponse,
)
async def get_sweep(
    sweep_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Owner-scoped fetch — used by the UI's progress poller."""
    from services import backtest_sweep_service as svc
    row = await svc.get_sweep(
        db, sweep_id=sweep_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    return _sweep_to_response(row)


@router.post(
    "/sweeps/{sweep_id}/start",
    response_model=BacktestSweepResponse,
)
async def start_sweep(
    sweep_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Fire the background worker. The HTTP request returns
    immediately — clients poll `GET /sweeps/{id}` for progress."""
    from services import backtest_sweep_service as svc

    row = await svc.get_sweep(
        db, sweep_id=sweep_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    if row.status != svc.STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Sweep is in status={row.status!r}; only pending "
                   "sweeps can be started.",
        )
    svc.start_sweep_in_background(row.id)
    # Re-fetch so the returned row reflects the worker's status flip
    # (best-effort — the worker may not have flipped to running yet
    # by the time we return; that's fine, the UI poll will catch it).
    refreshed = await svc.get_sweep(
        db, sweep_id=sweep_id, owner_id=_coerce_owner_uuid(user),
    )
    return _sweep_to_response(refreshed or row)


@router.post(
    "/sweeps/{sweep_id}/cancel",
    response_model=BacktestSweepResponse,
)
async def cancel_sweep(
    sweep_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Set the sweep's status to `cancelled`. Already-running
    workers see this on their next iteration check and bail.
    Already-terminal sweeps are no-op."""
    from services import backtest_sweep_service as svc

    row = await svc.get_sweep(
        db, sweep_id=sweep_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    cancelled = await svc.cancel_sweep(db, row)
    return _sweep_to_response(cancelled)


@router.delete(
    "/sweeps/{sweep_id}",
    status_code=204,
)
async def delete_sweep(
    sweep_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Hard-delete the sweep row. Spawned discussions are NOT
    cascaded — they remain inspectable. Callers should /cancel
    first if the sweep is actively running, otherwise the
    background worker will write to a deleted row and noop."""
    from services import backtest_sweep_service as svc

    row = await svc.get_sweep(
        db, sweep_id=sweep_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    await svc.delete_sweep(db, row)
    return None


# ── Strategy templates (PR-A) ────────────────────────────────────


@router.get(
    "/strategies",
    response_model=list[StrategyTemplateResponse],
)
async def list_strategies(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Most-recent-first list of the caller's strategy templates."""
    from services import strategy_template_service as tsvc
    rows = await tsvc.list_templates(
        db, owner_id=_coerce_owner_uuid(user),
    )
    return [_template_to_response(r) for r in rows]


@router.post(
    "/strategies",
    response_model=StrategyTemplateResponse,
    status_code=201,
)
async def create_strategy(
    body: StrategyTemplateCreate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from services import strategy_template_service as tsvc
    try:
        tmpl = await tsvc.create_template(
            db, owner_id=_coerce_owner_uuid(user),
            name=body.name,
            description=body.description,
            topic=body.topic,
            rules=body.rules,
            market=body.market,
            persona_ids=body.persona_ids,
            default_rounds=body.default_rounds,
            default_concurrency=body.default_concurrency,
            default_auto_post_mortem=body.default_auto_post_mortem,
            auto_schedule_enabled=body.auto_schedule_enabled,
            auto_schedule_cadence_hours=body.auto_schedule_cadence_hours,
            auto_schedule_anchor_offset_days=body.auto_schedule_anchor_offset_days,
            auto_schedule_trading_days_count=body.auto_schedule_trading_days_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _template_to_response(tmpl)


@router.get(
    "/strategies/{template_id}",
    response_model=StrategyTemplateResponse,
)
async def get_strategy(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return _template_to_response(row)


@router.patch(
    "/strategies/{template_id}",
    response_model=StrategyTemplateResponse,
)
async def update_strategy(
    template_id: uuid.UUID,
    body: StrategyTemplateUpdate,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    patch = body.model_dump(exclude_unset=True)
    try:
        updated = await tsvc.update_template(db, row, patch=patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _template_to_response(updated)


@router.get(
    "/sweeps/{sweep_id}/aggregate",
    response_model=SweepAggregateResponse,
)
async def aggregate_sweep_route(
    sweep_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Folded KPIs for a single sweep — verdict counts, win-rate,
    avg D1-D5 P&L, per-persona stats, recent post-mortem
    lessons. Empty payload (zero counts, null win_rate) when no
    spawned discussion has resolved yet."""
    from services import backtest_sweep_service as svc
    from services import sweep_aggregate_service as agg

    sweep = await svc.get_sweep(
        db, sweep_id=sweep_id, owner_id=_coerce_owner_uuid(user),
    )
    if sweep is None:
        raise HTTPException(status_code=404, detail="Sweep not found")
    payload = await agg.aggregate_sweep(db, sweep)
    return SweepAggregateResponse(**payload)


@router.get(
    "/strategies/{template_id}/aggregate",
    response_model=SweepAggregateResponse,
)
async def aggregate_strategy_route(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Cross-sweep KPIs for every sweep that referenced this
    template. Soft-deleted templates are still aggregatable so a
    purged strategy's history stays inspectable."""
    from services import strategy_template_service as tsvc
    from services import sweep_aggregate_service as agg

    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
        include_deleted=True,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    payload = await agg.aggregate_strategy(
        db, owner_id=_coerce_owner_uuid(user), strategy_id=template_id,
    )
    return SweepAggregateResponse(**payload)


@router.post(
    "/strategies/{template_id}/learn",
    response_model=PersonaWeightLearnResponse,
)
async def learn_strategy_weights(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-C: recompute persona weights from this template's
    aggregate history. Sweep completion auto-triggers this for
    its parent template; this manual endpoint exists so the
    operator can force a re-learn after editing the roster or
    importing external sweeps."""
    from services import persona_weight_learner
    try:
        result = await persona_weight_learner.learn_weights_for_strategy(
            db,
            owner_id=_coerce_owner_uuid(user),
            strategy_id=template_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return PersonaWeightLearnResponse(**result)


@router.delete(
    "/strategies/{template_id}",
    status_code=204,
)
async def delete_strategy(
    template_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Soft-delete: future sweeps that reference this template can
    still resolve `template.name` for display in the dashboard."""
    from services import strategy_template_service as tsvc
    row = await tsvc.get_template(
        db, template_id=template_id,
        owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await tsvc.soft_delete_template(db, row)
    return None


@router.post(
    "/strategies/{template_id}/walk-forward",
    response_model=WalkForwardPlanResponse,
)
async def kick_off_walk_forward(
    template_id: uuid.UUID,
    body: WalkForwardRequest,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """PR-A1 follow-up — operator-triggered walk-forward run.

    Resolves the rolling (train, test) fold plan synchronously
    (so 400 surfaces immediately if the archive can't reach the
    requested span) and detaches the orchestrator into the event
    loop. The HTTP request returns the plan + `started=True`; the
    actual sweep rows appear via `GET /sweeps?strategy_id=...` as
    the worker creates them.

    Failure modes:
      - 404 — strategy template doesn't exist or doesn't belong
        to the caller
      - 400 — anchor_date can't be parsed, or
        `plan_walk_forward` rejects the requested span (archive
        too thin, invalid combination)
    """
    from datetime import date as _date

    from services import strategy_template_service as tsvc
    from services import walk_forward_service as wf

    owner_uuid = _coerce_owner_uuid(user)
    tmpl = await tsvc.get_template(
        db, template_id=template_id, owner_id=owner_uuid,
    )
    if tmpl is None:
        raise HTTPException(
            status_code=404, detail="Strategy not found",
        )

    try:
        anchor = _date.fromisoformat(body.anchor_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"anchor_date must be ISO YYYY-MM-DD: {exc}",
        )

    try:
        plan = await wf.plan_walk_forward(
            db,
            strategy_id=template_id,
            market=tmpl.market,
            anchor_date=anchor,
            train_window_days=body.train_window_days,
            test_window_days=body.test_window_days,
            n_folds=body.n_folds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    wf.execute_walk_forward_in_background(
        owner_id=owner_uuid,
        strategy_id=template_id,
        plan=plan,
        rounds_per_discussion=body.rounds_per_discussion,
        concurrency=body.concurrency,
        auto_post_mortem=body.auto_post_mortem,
    )

    return WalkForwardPlanResponse(
        strategy_id=plan.strategy_id,
        market=plan.market,
        train_window_days=plan.train_window_days,
        test_window_days=plan.test_window_days,
        folds=[
            WalkForwardFoldOut(
                fold_index=f.fold_index,
                train_anchor=f.train_anchor.isoformat(),
                train_dates=[d.isoformat() for d in f.train_dates],
                test_anchor=f.test_anchor.isoformat(),
                test_dates=[d.isoformat() for d in f.test_dates],
            )
            for f in plan.folds
        ],
        started=True,
    )
