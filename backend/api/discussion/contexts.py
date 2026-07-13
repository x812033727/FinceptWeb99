"""Discussion per-round audit sub-router.

Owner-scoped read surfaces over a discussion's rounds: context
snapshots (replay/audit), per-round token usage (coarse + per-persona
detail), and the D1-D5 scoreboard. All quota-free reads — they don't
touch the `_refund` binding tests patch on `api.discussion.router`.

Mounted under `/api/discussion` so paths keep the `/api/discussion/...`
shape the frontend already calls — identical to before the split.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import CurrentUser, _coerce_owner_uuid
from api.discussion.schemas import ScoreboardResponse, ScoreboardRow
from db.session import get_db
from services import discussion_service

router = APIRouter()


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
            # R6 PR2 round digest — null unless the feature was enabled
            # when this round ran. Lets the UI show a per-round recap.
            "digest":      r.digest,
        }
        for r in rows
    ]


@router.get("/sessions/{discussion_id}/round-usage")
async def get_round_usage(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Per-round token tally (input + output) for a discussion. Owner-
    scoped, same access rule as the rest of the discussion endpoints.
    Returns `[{round, prompt_tokens, completion_tokens, total_tokens,
    cost_usd}, ...]` ordered by round. Empty for discussions that ran
    before per-round usage attribution was wired in (their usage rows
    carry NULL discussion_id/round)."""
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    from services import llm_usage_service
    return await llm_usage_service.discussion_round_usage(db, discussion_id=row.id)


@router.get("/sessions/{discussion_id}/round-usage/detail")
async def get_round_usage_detail(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Finer per-round ctx usage: exact per-persona token / cost / tool
    counts (from llm_usage_events) joined with each turn's prompt
    composition (`discussion_turns.input_breakdown` — char size per
    prompt section + per context block). Owner-scoped. Returns
    `[{round, persona_id, provider, model, prompt_tokens,
    completion_tokens, total_tokens, cost_usd, tool_call_count,
    breakdown}, ...]` ordered by (round, persona). `breakdown` is null
    for placeholder turns and rows recorded before the column existed."""
    row = await discussion_service.get_discussion(
        db, discussion_id=discussion_id, owner_id=_coerce_owner_uuid(user),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Discussion not found")
    from services import llm_usage_service
    usage = await llm_usage_service.discussion_round_persona_usage(
        db, discussion_id=row.id,
    )
    turns = await discussion_service.get_turns(db, discussion_id=row.id)
    bd_by_key = {
        (t.round, t.persona_id): t.input_breakdown
        for t in turns
        if getattr(t, "input_breakdown", None) is not None
    }
    for u in usage:
        u["breakdown"] = bd_by_key.get((u["round"], u["persona_id"]))
    return usage


@router.get(
    "/sessions/{discussion_id}/scoreboard",
    response_model=ScoreboardResponse,
)
async def get_scoreboard(
    discussion_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    debug: bool = False,
):
    """D1-D5 daily close + change % vs day-1 open per recommended
    symbol. Owner-scoped. Reads the persisted `daily_close_prices`
    column when populated (filled in by the daily 09:30 UTC cron);
    falls back to an on-demand compute against `ohlcv_daily` when
    NULL so newly-concluded discussions show partial data
    immediately instead of waiting a day.

    `?debug=true` adds a `debug` payload with cron eligibility,
    trading-window resolution, per-symbol archive/live-fallback
    trace, and the last cron-run snapshot — for "why is this
    scoreboard empty" investigations.
    """
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

    debug_traces: list[dict] | None = [] if debug else None
    payload = await discussion_scoreboard_service.compute_scoreboard(
        db, row, debug_traces=debug_traces,
    )
    debug_payload: dict | None = None
    if debug_traces is not None:
        debug_payload = await discussion_scoreboard_service.build_scoreboard_debug_payload(
            row, debug_traces,
        )
    return ScoreboardResponse(
        discussion_id=row.id,
        anchor_date=payload["anchor_date"],
        created_at_tw_date=payload["created_at_tw_date"],
        rows=[ScoreboardRow(**r) for r in payload["rows"]],
        debug=debug_payload,
    )
