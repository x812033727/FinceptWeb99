import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.decision_journal import DecisionJournalEntry
from models.discussion import Discussion
from models.user import User, UserRole
from services.decision_journal_service import calculate_outcomes, refresh_decision_journal


def _bars(count: int = 20) -> list[dict]:
    return [
        {"time": (date(2026, 6, 1) + timedelta(days=i)).isoformat(), "open": 100 + i, "close": 100 + i}
        for i in range(count)
    ]


def test_calculate_d1_d5_d20_cost_and_drawdown():
    bars = _bars()
    bars[5]["close"] = 90  # drawdown after a 104 peak
    result = calculate_outcomes(bars, transaction_cost_bps=15)
    assert result["entry_price"] == 100
    assert result["outcomes"]["d1"]["gross_return_pct"] == 0
    assert result["outcomes"]["d1"]["net_return_pct"] == -0.15
    assert result["outcomes"]["d5"]["net_return_pct"] == 3.85
    assert result["outcomes"]["d20"]["net_return_pct"] == 18.85
    assert result["max_drawdown_pct"] == pytest.approx(-13.4615)
    assert result["status"] == "resolved"


def test_calculate_partial_window_is_tracking():
    result = calculate_outcomes(_bars(5), transaction_cost_bps=15)
    assert result["outcomes"]["d5"]["resolved"] is True
    assert result["outcomes"]["d20"]["resolved"] is False
    assert result["status"] == "tracking"
    assert result["observations"] == 5


@pytest.mark.asyncio
async def test_refresh_projects_discussion_and_is_idempotent(db_session: AsyncSession):
    user = User(email="journal-refresh@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    discussion = Discussion(
        owner_id=user.id, topic="t", rules="r", persona_ids=["a", "b"], market="TW",
        status="done", current_round=1, auto_run=True,
        conclusion={"recommendations": [{"symbol": "2330", "confidence": 0.7}]},
        created_at=datetime(2026, 6, 1, tzinfo=UTC), updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db_session.add(discussion)
    await db_session.commit()

    with patch(
        "services.decision_journal_service.read_ohlcv_range_autosession",
        new=AsyncMock(return_value=_bars()),
    ):
        assert await refresh_decision_journal(db_session) == 1
        assert await refresh_decision_journal(db_session) == 0  # resolved entries are immutable

    rows = list((await db_session.scalars(select(DecisionJournalEntry))).all())
    assert len(rows) == 1
    assert rows[0].user_id == user.id
    assert rows[0].source_type == "paper_recommendation"
    assert rows[0].outcomes["d20"]["resolved"] is True


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_decision_journal_api_is_owner_scoped_and_summarized(
    client: AsyncClient, db_session: AsyncSession,
):
    mine_token = await _token(client, "journal-mine@test.com")
    other_token = await _token(client, "journal-other@test.com")
    users = list((await db_session.scalars(select(User).where(
        User.email.in_(["journal-mine@test.com", "journal-other@test.com"])
    ))).all())
    by_email = {user.email: user for user in users}
    for email, symbol, net in (
        ("journal-mine@test.com", "2330", 8.0),
        ("journal-other@test.com", "AAPL", -5.0),
    ):
        db_session.add(DecisionJournalEntry(
            user_id=by_email[email].id, source_type="paper_recommendation",
            source_id=str(uuid.uuid4()), market="TW" if symbol == "2330" else "US",
            symbol=symbol, prediction_at=datetime.now(UTC), anchor_date=date.today(),
            entry_price=100, outcomes={
                "d1": {"resolved": True, "net_return_pct": net},
                "d5": {"resolved": True, "net_return_pct": net},
                "d20": {"resolved": True, "net_return_pct": net},
            }, max_drawdown_pct=-2, observations=20, status="resolved",
        ))
    await db_session.commit()

    response = await client.get(
        "/api/decision-journal", headers={"Authorization": f"Bearer {mine_token}"},
    )
    assert response.status_code == 200
    assert [row["symbol"] for row in response.json()["entries"]] == ["2330"]
    assert response.json()["summary"]["horizons"]["d20"] == {
        "sample_size": 1, "win_rate_pct": 100.0, "average_net_return_pct": 8.0,
    }
    other = await client.get(
        "/api/decision-journal", headers={"Authorization": f"Bearer {other_token}"},
    )
    assert [row["symbol"] for row in other.json()["entries"]] == ["AAPL"]
