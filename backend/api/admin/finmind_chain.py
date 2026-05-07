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
Admin = Annotated[User, Depends(require_admin)]


class ChainStatePayload(BaseModel):
    """Mirrors `services.finmind_chain_service.ChainState` plus the
    augmented quota / host-detection fields from `get_state()`."""

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
    quota_used: int | None
    quota_limit: int
    host_chain_likely_active: bool
    default_datasets: list[str]


class StartRequest(BaseModel):
    datasets: list[str] = Field(
        default_factory=lambda: list(chain.DEFAULT_DATASETS),
        description=(
            "Dataset codes to enqueue. Defaults to the 12-dataset list "
            "from finmind_chain.sh."
        ),
    )
    days: int = Field(
        default=3650,
        ge=1,
        le=3650 * 2,
        description="Days back from today to backfill. Default 10 years.",
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
async def get_chain(_: Admin) -> ChainStatePayload:
    return ChainStatePayload(**(await chain.get_state()))


@router.post(
    "/chain/start",
    response_model=ChainStatePayload,
    summary="AdminPage: start the 12-dataset 10-year backfill chain",
)
async def post_start(body: StartRequest, _: Admin) -> ChainStatePayload:
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
async def post_stop(_: Admin) -> ChainStatePayload:
    return ChainStatePayload(**(await chain.stop_chain()))


@router.post(
    "/chain/reset-stuck",
    response_model=ResetStuckResponse,
    summary="AdminPage: flip stale running chunks back to pending",
)
async def post_reset_stuck(_: Admin) -> ResetStuckResponse:
    n = await chain.reset_stuck()
    return ResetStuckResponse(reset=n)
