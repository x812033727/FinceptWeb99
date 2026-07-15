import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.decision_journal import DecisionJournalEntry
from models.stock_pick_run import StockPickRun
from models.stock_report import StockReport
from models.user import User, UserRole
from services.daily_pick_service import NoEligibleCandidatesError, generate_daily_picks
from services.decision_journal_service import refresh_decision_journal


def _report(
    user_id: uuid.UUID,
    *,
    symbol: str,
    market: str = "TW",
    conclusion: str = "偏多觀察，營運動能延續。",
    quality: float = 0.9,
    created_at: datetime,
) -> StockReport:
    return StockReport(
        user_id=user_id,
        symbol=symbol,
        market=market,
        content_md=f"## 結論\n{conclusion}",
        model="test",
        model_id="test",
        prompt_version="stock-report-v3-source-quality",
        evidence=[{
            "id": "E1", "path": "fundamentals.pe", "value": 18,
            "source": "twse", "as_of": "2026-07-15",
        }],
        context_snapshot={
            "quality_summary": {
                "reliability_score": quality,
                "band": "high" if quality >= 0.9 else "moderate",
                "issue_counts": {"conflict": 0},
            },
        },
        quality_score=quality,
        sections={"結論": conclusion},
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_generate_daily_picks_is_ranked_traceable_and_idempotent(
    db_session: AsyncSession,
):
    now = datetime(2026, 7, 15, 9, tzinfo=UTC)
    user = User(email="daily-picks@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    db_session.add_all([
        _report(user.id, symbol="2330", quality=0.95, created_at=now - timedelta(hours=1)),
        _report(user.id, symbol="2454", quality=0.80, created_at=now - timedelta(days=1)),
        _report(
            user.id, symbol="2317", quality=0.99,
            conclusion="偏空觀察，需求仍弱。", created_at=now,
        ),
        _report(user.id, symbol="2603", quality=0.60, created_at=now),
        _report(user.id, symbol="6505", quality=0.99, created_at=now - timedelta(days=8)),
    ])
    await db_session.commit()

    run = await generate_daily_picks(db_session, user.id, "TW", now=now)
    repeated = await generate_daily_picks(db_session, user.id, "TW", now=now)

    assert repeated.id == run.id
    assert run.methodology_version == "trusted-report-ranking-v1"
    assert [item["symbol"] for item in run.candidates] == ["2330", "2454"]
    assert [item["rank"] for item in run.candidates] == [1, 2]
    assert run.candidates[0]["evidence"][0]["id"] == "E1"
    assert run.candidates[0]["source_report_id"] in run.source_report_ids
    assert len(list((await db_session.scalars(select(StockPickRun))).all())) == 1

    journal = list((await db_session.scalars(select(DecisionJournalEntry).where(
        DecisionJournalEntry.source_type == "ai_stock_pick",
    ).order_by(DecisionJournalEntry.symbol))).all())
    assert [row.symbol for row in journal] == ["2330", "2454"]
    assert {row.source_id for row in journal} == {str(run.id)}
    assert {row.anchor_date.isoformat() for row in journal} == {"2026-07-16"}
    assert all(row.status == "pending" for row in journal)


@pytest.mark.asyncio
async def test_generate_daily_picks_requires_recent_high_quality_bullish_report(
    db_session: AsyncSession,
):
    now = datetime(2026, 7, 15, tzinfo=UTC)
    user = User(email="daily-picks-empty@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    db_session.add(_report(
        user.id, symbol="AAPL", market="US", conclusion="中性觀察。",
        quality=0.99, created_at=now,
    ))
    await db_session.commit()

    with pytest.raises(NoEligibleCandidatesError):
        await generate_daily_picks(db_session, user.id, "US", now=now)
    assert await db_session.scalar(select(StockPickRun)) is None


@pytest.mark.asyncio
async def test_refresh_decision_journal_resolves_daily_pick_entries(
    db_session: AsyncSession,
):
    now = datetime(2026, 7, 15, tzinfo=UTC)
    user = User(email="daily-picks-journal@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    entry = DecisionJournalEntry(
        user_id=user.id,
        source_type="ai_stock_pick",
        source_id=str(uuid.uuid4()),
        market="US",
        symbol="AAPL",
        prediction_at=now,
        anchor_date=now.date(),
        confidence=0.8,
    )
    db_session.add(entry)
    await db_session.commit()
    bars = [
        {"time": f"2026-07-{day:02d}", "open": 100 if day == 15 else 101, "close": 100 + day - 14}
        for day in range(15, 35)
    ]

    with patch(
        "services.decision_journal_service.read_ohlcv_range_autosession",
        new=AsyncMock(return_value=bars),
    ):
        changed = await refresh_decision_journal(db_session)

    assert changed == 1
    await db_session.refresh(entry)
    assert entry.status == "resolved"
    assert entry.entry_price == 100
    assert entry.outcomes["d20"]["resolved"] is True


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_daily_pick_api_is_owner_scoped_and_surfaces_latest(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "daily-picks-api@test.com"
    token = await _token(client, email)
    stranger = await _token(client, "daily-picks-stranger@test.com")
    user = await db_session.scalar(select(User).where(User.email == email))
    db_session.add(_report(
        user.id, symbol="2330", quality=0.9, created_at=datetime.now(UTC),
    ))
    await db_session.commit()

    generated = await client.post(
        "/api/research/daily-picks/generate?market=TW",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert generated.status_code == 201
    assert generated.json()["candidates"][0]["symbol"] == "2330"

    latest = await client.get(
        "/api/research/daily-picks/latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert latest.status_code == 200
    assert latest.json()["runs"][0]["methodology_version"] == "trusted-report-ranking-v1"
    assert "not investment advice" in latest.json()["disclaimer"]

    stranger_latest = await client.get(
        "/api/research/daily-picks/latest",
        headers={"Authorization": f"Bearer {stranger}"},
    )
    assert stranger_latest.json()["runs"] == []


@pytest.mark.asyncio
async def test_daily_pick_api_explains_missing_eligible_reports(client: AsyncClient):
    token = await _token(client, "daily-picks-api-empty@test.com")
    response = await client.post(
        "/api/research/daily-picks/generate?market=US",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert "No eligible bullish reports" in response.json()["detail"]
