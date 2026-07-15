import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_viewer
from db.session import get_db
from services.decision_journal_service import list_entries, summarize

router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]
DB = Annotated[AsyncSession, Depends(get_db)]


def _entry(row) -> dict[str, Any]:
    return {
        "id": str(row.id), "source_type": row.source_type, "source_id": row.source_id,
        "market": row.market, "symbol": row.symbol, "prediction_at": row.prediction_at,
        "anchor_date": row.anchor_date, "stance": row.stance, "confidence": row.confidence,
        "entry_price": row.entry_price, "outcomes": row.outcomes,
        "max_drawdown_pct": row.max_drawdown_pct,
        "transaction_cost_bps": row.transaction_cost_bps,
        "observations": row.observations, "status": row.status, "updated_at": row.updated_at,
    }


@router.get("")
async def decision_journal(
    user: CurrentUser, db: DB, limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    rows = await list_entries(db, uuid.UUID(user["id"]), limit=limit)
    return {"entries": [_entry(row) for row in rows], "summary": summarize(rows)}
