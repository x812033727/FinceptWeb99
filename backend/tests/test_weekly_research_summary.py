import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.alert import AlertEvent
from models.decision_journal import DecisionJournalEntry
from models.investment_thesis import InvestmentThesis, ThesisEvent
from models.stock_report import StockReport
from models.stock_pick_run import StockPickRun
from models.user import User


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_weekly_summary_is_owner_scoped_and_surfaces_pending(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "weekly-owner@test.com"
    token = await _token(client, email)
    stranger_token = await _token(client, "weekly-stranger@test.com")
    owner = await db_session.scalar(select(User).where(User.email == email))
    stranger = await db_session.scalar(select(User).where(User.email == "weekly-stranger@test.com"))
    now = datetime.now(UTC)
    thesis = InvestmentThesis(
        user_id=owner.id, market="TW", symbol="2330", title="Foundry", core_case="case",
        review_date=date.today() - timedelta(days=1),
    )
    db_session.add(thesis)
    await db_session.flush()
    db_session.add_all([
        ThesisEvent(
            thesis_id=thesis.id, user_id=owner.id, event_type="news", title="New order",
            source="archive", source_ref="weekly-news-1", occurred_at=now,
        ),
        ThesisEvent(
            thesis_id=thesis.id, user_id=owner.id,
            event_type="watch_condition_triggered",
            title="Watch condition triggered: Revenue growth floor",
            details={
                "condition": {"label": "Revenue growth floor"},
                "observed_value": 7.5,
            },
            source="thesis_watch_engine", source_ref="weekly-watch-1", occurred_at=now,
        ),
        AlertEvent(
            user_id=owner.id, symbol="2330", market="TW", kind="price",
            message="breakout", fired_at=now,
        ),
        StockReport(
            user_id=owner.id, symbol="2330", market="TW", content_md="report",
            model="test", model_id="test", prompt_version="v", evidence=[], quality_score=1.0,
            created_at=now,
        ),
        StockPickRun(
            user_id=owner.id, market="TW", run_date=date.today(),
            candidate_count=2, candidates=[], source_report_ids=[], generated_at=now,
        ),
        DecisionJournalEntry(
            user_id=owner.id, source_type="paper_recommendation", source_id=str(uuid.uuid4()),
            market="TW", symbol="2330", prediction_at=now, anchor_date=date.today(),
            confidence=0.8, outcomes={
                "d5": {"resolved": True, "net_return_pct": 4.0},
                "d20": {"resolved": False, "net_return_pct": None},
            }, observations=5, status="tracking",
        ),
        AlertEvent(
            user_id=stranger.id, symbol="AAPL", market="US", kind="price",
            message="private", fired_at=now,
        ),
    ])
    await db_session.commit()

    response = await client.get(
        "/api/research/weekly-summary", headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["theses"]["events"] == 2
    assert data["alerts"]["count"] == 1
    assert data["ai"]["reports_generated"] == 1
    assert data["ai"]["d5_brier_score"] == 0.04
    assert data["ai"]["daily_pick_runs"] == 1
    assert data["ai"]["daily_candidates"] == 2
    assert data["pending"]["thesis_reviews"][0]["symbol"] == "2330"
    assert data["pending"]["decision_outcomes"][0]["status"] == "tracking"
    assert data["pending"]["watch_triggers"] == [{
        "thesis_id": str(thesis.id),
        "title": "Watch condition triggered: Revenue growth floor",
        "condition": {"label": "Revenue growth floor"},
        "observed_value": 7.5,
        "occurred_at": now.isoformat(),
    }]

    stranger_response = await client.get(
        "/api/research/weekly-summary",
        headers={"Authorization": f"Bearer {stranger_token}"},
    )
    assert stranger_response.json()["alerts"]["count"] == 1
    assert stranger_response.json()["theses"]["events"] == 0
    assert stranger_response.json()["ai"]["reports_generated"] == 0
    assert stranger_response.json()["ai"]["daily_candidates"] == 0
