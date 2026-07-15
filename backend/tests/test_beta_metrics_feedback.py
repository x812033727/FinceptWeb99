from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertCondition, PriceAlert
from models.auth_security import AuthInvitation
from models.data_quality_feedback import DataQualityFeedback
from models.governance import AuditEvent
from models.investment_thesis import InvestmentThesis
from models.stock_report import StockReport
from models.user import User, UserRole


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_data_quality_feedback_and_admin_beta_metrics(
    client: AsyncClient, db_session: AsyncSession,
):
    admin_email = "beta-admin@test.com"
    beta_email = "beta-user@test.com"
    admin_token = await _token(client, admin_email)
    beta_token = await _token(client, beta_email)
    admin = await db_session.scalar(select(User).where(User.email == admin_email))
    beta_user = await db_session.scalar(select(User).where(User.email == beta_email))
    admin.role = UserRole.admin
    await db_session.commit()
    admin_token = (await client.post(
        "/api/auth/login", json={"email": admin_email, "password": "Pass99!!"},
    )).json()["access_token"]

    feedback = await client.post(
        "/api/feedback/data-quality",
        headers={"Authorization": f"Bearer {beta_token}"},
        json={
            "market": "TW", "symbol": "2330", "category": "stale",
            "description": "Quote timestamp is older than the displayed session.",
            "endpoint": "/api/tw/quote/2330",
            "observed_meta": {"freshness": "stale", "source": "db_stale"},
        },
    )
    assert feedback.status_code == 201
    row = await db_session.scalar(select(DataQualityFeedback))
    assert row.user_id == beta_user.id
    assert row.symbol == "2330"

    now = datetime.now(UTC)
    db_session.add_all([
        AuthInvitation(
            email=beta_email, token_hash="d" * 64, role="viewer", invited_by=admin.id,
            expires_at=now + timedelta(days=1), used_at=now,
        ),
        PriceAlert(
            user_id=beta_user.id, symbol="2330", market="TW",
            condition=AlertCondition.above, target_price=100,
        ),
        InvestmentThesis(
            user_id=beta_user.id, market="TW", symbol="2330", title="Foundry", core_case="case",
        ),
        StockReport(
            user_id=beta_user.id, symbol="2330", market="TW", content_md="report",
            model="test", model_id="test", prompt_version="v", evidence=[], quality_score=1,
            created_at=now,
        ),
        AuditEvent(
            actor_user_id=beta_user.id,
            action="POST /api/ai/stock-report/{market}/{symbol}",
            resource_type="ai", resource_id="2330", outcome="success",
            event_metadata={"status_code": 200}, created_at=now,
        ),
    ])
    await db_session.commit()

    forbidden = await client.get(
        "/api/admin/beta/metrics", headers={"Authorization": f"Bearer {beta_token}"},
    )
    assert forbidden.status_code == 403
    metrics = await client.get(
        "/api/admin/beta/metrics", headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert metrics.status_code == 200
    data = metrics.json()
    assert data["invited"] == 1
    assert data["activated"] == 1
    assert data["activation_rate_pct"] == 100
    assert data["reports_completed"] == 1
    assert data["report_completion_rate_pct"] == 100
    assert data["alert_adoption_rate_pct"] == 100
    assert data["core_workflow_completion_rate_pct"] == 100
    assert data["data_quality_feedback"] == {"total": 1, "open": 1}
