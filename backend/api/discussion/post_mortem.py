"""Discussion post-mortem sub-router.

Backtest self-critique: injects a self-critique prompt against the
actual next-trading-day top gainers so the next round of personas has
to defend or revise their recommendation against ground truth. Only
valid for backtest discussions (`as_of_date` set) that already have a
conclusion. Consumes no AI quota itself — the LLM cost happens in the
subsequent `/round` call — so it doesn't touch the `_refund` binding
tests patch on `api.discussion.router`.

Mounted under `/api/discussion` so paths keep the `/api/discussion/...`
shape the frontend already calls — identical to before the split.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import CurrentUser, _coerce_owner_uuid
from api.discussion.schemas import (
    PostMortemDailyGainersOut,
    PostMortemDayPerformanceOut,
    PostMortemGainerOut,
    PostMortemRecommendedPerformanceOut,
    PostMortemResponse,
    PostMortemVerdictOut,
    PostMortemWinnerOut,
)
from db.session import get_db
from services import discussion_service

router = APIRouter()


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
