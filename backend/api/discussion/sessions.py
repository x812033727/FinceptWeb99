"""Discussion session CRUD sub-router.

Owns the core session lifecycle surface: list / create / detail /
edit (PATCH, draft-only) / owner-only cascade delete. Quota-free —
none of these endpoints reserve or refund AI credits, so they don't
depend on the `_refund` binding tests patch on `api.discussion.router`.

Mounted under `/api/discussion` so paths keep the `/api/discussion/...`
shape the frontend already calls — identical to before the split.
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.discussion._helpers import (
    CurrentUser,
    _coerce_owner_uuid,
    _to_response,
)
from api.discussion.schemas import (
    CreateDiscussionRequest,
    DiscussionDetailResponse,
    DiscussionResponse,
    TurnResponse,
    UpdateDiscussionRequest,
)
from db.session import get_db
from services import discussion_service

log = logging.getLogger(__name__)
router = APIRouter()


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
    except HTTPException:
        raise
    except Exception as exc:
        # Anything unexpected (DB schema drift, FK resolution, IntegrityError,
        # response-model coercion failure) used to bubble up as a bare
        # `Internal Server Error` with no detail — leaving the user's red
        # banner uninformative and the operator with only a stack trace
        # buried in container logs. Log the full traceback for diagnosis,
        # and surface the exception class + message so the user can quote
        # something useful when reporting the failure.
        log.exception(
            "discussion.create.unexpected_failure",
            extra={
                "user_id": user.get("id") if isinstance(user, dict) else None,
                "market": body.market,
                "persona_count": len(body.persona_ids),
                "as_of_date": body.as_of_date,
            },
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create discussion: {type(exc).__name__}: {exc}",
        )
    try:
        return _to_response(row)
    except Exception as exc:
        # Response-model coercion can fail if a recently-added column was
        # left out of the local DB schema (a missing migration leaves
        # `db.refresh` returning a row without the attribute). The row is
        # already persisted; the user's click DID succeed. Log the
        # specifics so the operator can apply the missing migration; the
        # 500 still surfaces because we can't honour the response_model
        # contract.
        log.exception(
            "discussion.create.response_serialization_failed",
            extra={
                "discussion_id": str(getattr(row, "id", None)),
                "user_id": user.get("id") if isinstance(user, dict) else None,
            },
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Discussion was created but the response could not be "
                f"serialized: {type(exc).__name__}: {exc}"
            ),
        )


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
            injected_by_user=bool(getattr(t, "injected_by_user", False)),
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
