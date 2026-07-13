from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from services import llm_usage_service as usage

from ..schemas import (
    ToolCallStatOut,
    UsageBucketOut,
    UsageDayPoint,
    UsageSummaryOut,
)

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── LLM usage summary (admin-wide) ───────────────────────────────

def _summary_to_schema(s: usage.UsageSummary) -> UsageSummaryOut:
    return UsageSummaryOut(
        range_days=s.range_days,
        user_scoped=s.user_scoped,
        total_requests=s.total_requests,
        total_prompt_tokens=s.total_prompt_tokens,
        total_completion_tokens=s.total_completion_tokens,
        total_cost_usd=s.total_cost_usd,
        by_provider=[UsageBucketOut(**b.__dict__) for b in s.by_provider],
        by_day=[UsageDayPoint(**d) for d in s.by_day],
        total_tool_calls=s.total_tool_calls,
        top_tools=[
            ToolCallStatOut(name=t["name"], count=t["count"])
            for t in s.top_tools
        ],
    )


@router.get("/llm-usage", response_model=UsageSummaryOut)
async def admin_llm_usage(
    _: AdminUser, db: DB, range_days: int = 30,
) -> UsageSummaryOut:
    """System-wide LLM usage aggregate for the last `range_days` days."""
    range_days = max(1, min(range_days, 365))
    summary = await usage.usage_summary(db, range_days=range_days)
    return _summary_to_schema(summary)
