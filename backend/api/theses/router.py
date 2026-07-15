import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.theses.schemas import ThesisCreate, ThesisEventOut, ThesisOut, ThesisReview, ThesisUpdate
from auth.permissions import require_viewer
from db.session import get_db
from models.investment_thesis import InvestmentThesis, ThesisEvent

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


async def _owned(db: AsyncSession, thesis_id: uuid.UUID, user_id: str) -> InvestmentThesis:
    row = await db.scalar(select(InvestmentThesis).where(InvestmentThesis.id == thesis_id, InvestmentThesis.user_id == uuid.UUID(user_id)))
    if row is None:
        raise HTTPException(status_code=404, detail="Thesis not found")
    return row


@router.get("", response_model=list[ThesisOut])
async def list_theses(user: CurrentUser, db: DB):
    rows = await db.scalars(select(InvestmentThesis).where(InvestmentThesis.user_id == uuid.UUID(user["id"])).order_by(InvestmentThesis.updated_at.desc()))
    return list(rows)


@router.post("", response_model=ThesisOut, status_code=status.HTTP_201_CREATED)
async def create_thesis(body: ThesisCreate, user: CurrentUser, db: DB):
    row = InvestmentThesis(user_id=uuid.UUID(user["id"]), **body.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


@router.get("/{thesis_id}", response_model=ThesisOut)
async def get_thesis(thesis_id: uuid.UUID, user: CurrentUser, db: DB):
    return await _owned(db, thesis_id, user["id"])


@router.patch("/{thesis_id}", response_model=ThesisOut)
async def update_thesis(thesis_id: uuid.UUID, body: ThesisUpdate, user: CurrentUser, db: DB):
    row = await _owned(db, thesis_id, user["id"])
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(row)
    return row


@router.delete("/{thesis_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thesis(thesis_id: uuid.UUID, user: CurrentUser, db: DB):
    row = await _owned(db, thesis_id, user["id"])
    await db.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{thesis_id}/review", response_model=ThesisEventOut, status_code=status.HTTP_201_CREATED)
async def review_thesis(thesis_id: uuid.UUID, body: ThesisReview, user: CurrentUser, db: DB):
    row = await _owned(db, thesis_id, user["id"])
    now = datetime.now(UTC)
    row.last_reviewed_at = now
    row.review_date = body.next_review_date
    row.updated_at = now
    if body.conclusion == "invalidated":
        row.status = "invalidated"
    event = ThesisEvent(thesis_id=row.id, user_id=row.user_id, event_type="review", title=f"Thesis review: {body.conclusion}", details=body.model_dump(mode="json"), source="user", occurred_at=now)
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return event


@router.get("/{thesis_id}/timeline", response_model=list[ThesisEventOut])
async def thesis_timeline(thesis_id: uuid.UUID, user: CurrentUser, db: DB):
    await _owned(db, thesis_id, user["id"])
    rows = await db.scalars(select(ThesisEvent).where(ThesisEvent.thesis_id == thesis_id, ThesisEvent.user_id == uuid.UUID(user["id"])).order_by(ThesisEvent.occurred_at.desc()))
    return list(rows)
