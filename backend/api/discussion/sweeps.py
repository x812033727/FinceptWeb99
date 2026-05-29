"""Discussion backtest-sweep sub-router (PR #274).

CRUD + worker-lifecycle (start / cancel) + per-sweep aggregate. A
sweep is a fan-out of N discussions (one per trading day in the
resolved date list) sharing topic/rules/personas, driven by a
detached background worker.

Mounted under `/api/discussion` so paths read like
`/api/discussion/sweeps/...` — identical to before the split.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import (
    CurrentUser,
    _coerce_owner_uuid,
    _sweep_to_response,
)
from api.discussion.schemas import (
    BacktestSweepCreate,
    BacktestSweepResponse,
    SweepAggregateResponse,
)
from db.session import get_db

router = APIRouter()


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
