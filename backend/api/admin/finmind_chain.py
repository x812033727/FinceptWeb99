"""Admin endpoints for the FinMind backfill chain.

Mounted under `/api/admin/finmind/chain*` so the frontend
FinmindBackfillCard can drive the chain via the same JWT admin auth
the rest of /admin uses.

See `services/finmind_chain_service.py` for the orchestration
contract.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth.permissions import require_admin
from models.user import User
from services import finmind_chain_service as chain

log = logging.getLogger("api.admin.finmind_chain")

router = APIRouter()
AdminUser = Annotated[User, Depends(require_admin)]


class PerDatasetProgress(BaseModel):
    """One row of the per-dataset breakdown surfaced on the AdminPage
    card. `row_count` is an estimate (pg_class.reltuples) — accurate
    enough for a progress meter without scanning multi-million-row
    finmind tables on every 3 s poll."""

    dataset: str
    local_table: str | None
    chunks_done: int
    chunks_failed: int
    chunks_pending: int
    chunks_running: int
    chunks_total: int
    row_count: int | None


class ChainStatePayload(BaseModel):
    """Mirrors `services.finmind_chain_service.ChainState` plus the
    augmented quota / external-activity / overall-progress fields
    that `get_state()` computes on read."""

    status: str
    queue: list[str]
    current_dataset: str | None
    current_symbol: str | None
    chunks_done: int
    chunks_total: int
    chunks_failed: int
    started_at: str | None
    last_chunk_at: str | None
    stop_requested: bool
    recent_errors: list[str]
    selected_datasets: list[str]
    universe_size: int
    quota_used: int | None
    quota_limit: int
    quota_limit_global: int
    external_activity_detected: bool
    default_datasets: list[str]
    total_chunks_done: int
    total_chunks_total: int
    per_dataset_progress: list[PerDatasetProgress]


class StartRequest(BaseModel):
    datasets: list[str] = Field(
        default_factory=lambda: list(chain.DEFAULT_DATASETS),
        description=(
            "Dataset codes to enqueue. Defaults to the 12-dataset list "
            "from finmind_chain.sh."
        ),
    )
    days: int = Field(
        default=365,
        ge=1,
        le=3650 * 2,
        description="Days back from today to backfill. Default 1 year.",
    )
    reset_stuck_first: bool = Field(
        default=True,
        description=(
            "Flip stale running chunks back to pending before starting "
            "(recommended after a backend restart)."
        ),
    )


class ResetStuckResponse(BaseModel):
    reset: int


@router.get(
    "/chain",
    response_model=ChainStatePayload,
    summary="AdminPage: live chain state + quota gauge",
)
async def get_chain(_: AdminUser) -> ChainStatePayload:
    return ChainStatePayload(**(await chain.get_state()))


@router.post(
    "/chain/start",
    response_model=ChainStatePayload,
    summary="AdminPage: start the 12-dataset 10-year backfill chain",
)
async def post_start(body: StartRequest, _: AdminUser) -> ChainStatePayload:
    try:
        state = await chain.start_chain(
            datasets=body.datasets,
            days=body.days,
            reset_stuck_first=body.reset_stuck_first,
        )
    except chain.ChainAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except chain.ChainConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        )
    return ChainStatePayload(**state)


@router.post(
    "/chain/stop",
    response_model=ChainStatePayload,
    summary="AdminPage: soft-stop (current chunk finishes, then exit)",
)
async def post_stop(_: AdminUser) -> ChainStatePayload:
    return ChainStatePayload(**(await chain.stop_chain()))


@router.post(
    "/chain/reset-stuck",
    response_model=ResetStuckResponse,
    summary="AdminPage: flip stale running chunks back to pending",
)
async def post_reset_stuck(_: AdminUser) -> ResetStuckResponse:
    n = await chain.reset_stuck()
    return ResetStuckResponse(reset=n)
