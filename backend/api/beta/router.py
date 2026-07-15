import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin, require_viewer
from db.session import get_db
from models.alert import PriceAlert
from models.auth_security import AuthInvitation
from models.data_quality_feedback import DataQualityFeedback
from models.governance import AuditEvent
from models.investment_thesis import InvestmentThesis
from models.stock_report import StockReport

feedback_router = APIRouter()
admin_router = APIRouter()
DB = Annotated[AsyncSession, Depends(get_db)]


class DataQualityFeedbackCreate(BaseModel):
    market: Literal["TW", "US", "GLOBAL", "CRYPTO"]
    symbol: str | None = Field(default=None, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    category: Literal["stale", "missing", "conflict", "incorrect", "other"]
    description: str = Field(min_length=5, max_length=4000)
    endpoint: str | None = Field(default=None, max_length=300)
    observed_meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        return value.upper() if value else value


@feedback_router.post("/data-quality", status_code=status.HTTP_201_CREATED)
async def report_data_quality(
    body: DataQualityFeedbackCreate,
    user: Annotated[dict, Depends(require_viewer)], db: DB,
):
    row = DataQualityFeedback(user_id=uuid.UUID(user["id"]), **body.model_dump())
    db.add(row)
    await db.flush()
    return {"id": str(row.id), "status": row.status}


@admin_router.get("/metrics")
async def beta_metrics(
    admin: Annotated[dict, Depends(require_admin)], db: DB,  # noqa: ARG001
    days: Annotated[int, Query(ge=1, le=90)] = 7,
):
    since = datetime.now(UTC) - timedelta(days=days)
    invited = int(await db.scalar(select(func.count()).select_from(AuthInvitation)) or 0)
    activated = int(await db.scalar(select(func.count()).select_from(AuthInvitation).where(AuthInvitation.used_at.is_not(None))) or 0)
    weekly_active = int(await db.scalar(select(func.count(distinct(AuditEvent.actor_user_id))).where(
        AuditEvent.actor_user_id.is_not(None), AuditEvent.created_at >= since, AuditEvent.outcome == "success",
    )) or 0)
    report_attempts = int(await db.scalar(select(func.count()).select_from(AuditEvent).where(
        AuditEvent.created_at >= since,
        AuditEvent.action == "POST /api/ai/stock-report/{market}/{symbol}",
    )) or 0)
    reports = int(await db.scalar(select(func.count()).select_from(StockReport).where(StockReport.created_at >= since)) or 0)
    alert_users = int(await db.scalar(select(func.count(distinct(PriceAlert.user_id)))) or 0)
    report_users = set((await db.scalars(select(distinct(StockReport.user_id)))).all())
    thesis_users = set((await db.scalars(select(distinct(InvestmentThesis.user_id)))).all())
    alert_user_ids = set((await db.scalars(select(distinct(PriceAlert.user_id)))).all())
    workflow_users = len(report_users & thesis_users & alert_user_ids)
    feedback_total = int(await db.scalar(select(func.count()).select_from(DataQualityFeedback)) or 0)
    feedback_open = int(await db.scalar(select(func.count()).select_from(DataQualityFeedback).where(DataQualityFeedback.status == "open")) or 0)
    return {
        "window_days": days,
        "invited": invited, "activated": activated,
        "activation_rate_pct": round(activated / invited * 100, 2) if invited else None,
        "weekly_active_users": weekly_active,
        "report_attempts": report_attempts, "reports_completed": reports,
        "report_completion_rate_pct": round(reports / report_attempts * 100, 2) if report_attempts else None,
        "alert_adoption_users": alert_users,
        "alert_adoption_rate_pct": round(alert_users / activated * 100, 2) if activated else None,
        "core_workflow_users": workflow_users,
        "core_workflow_completion_rate_pct": round(workflow_users / activated * 100, 2) if activated else None,
        "data_quality_feedback": {"total": feedback_total, "open": feedback_open},
        "targets": {"activation_rate_pct": 70, "weekly_active_users": 10, "core_workflow_completion_rate_pct": 60},
    }
