from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.corporate_announcement import CorporateAnnouncement
from models.investment_thesis import InvestmentThesis, ThesisEvent
from models.news_article import NewsArticle
from models.tw_chip_metrics import TwInstitutionalDaily
from models.tw_revenue_monthly import TwRevenueMonthly
from models.user import User, UserRole
from tasks.sync_thesis_events import sync_thesis_events


@pytest.mark.asyncio
async def test_sync_projects_archives_to_owner_timeline_idempotently(db_session: AsyncSession):
    now = datetime(2026, 7, 15, 8, tzinfo=UTC)
    user = User(email="thesis-sync@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    thesis = InvestmentThesis(
        user_id=user.id, market="TW", symbol="2330", title="Foundry", core_case="case",
    )
    db_session.add(thesis)
    db_session.add_all([
        CorporateAnnouncement(
            market="TW", symbol="2330", announced_at=now, category="重大訊息",
            title="Board approves capex", body=None, source_url="https://example.test/a",
            source="mops", dedup_hash="a" * 64,
        ),
        NewsArticle(
            market="TW", symbol="2330", published_at=now, title="Demand remains strong",
            link="https://example.test/n", publisher="Wire", summary=None, payload=None,
            source="finmind", dedup_hash="b" * 64,
        ),
        TwRevenueMonthly(
            market="TW", symbol="2330", ts=date(2026, 7, 1), revenue=1000,
            revenue_yoy=Decimal("25.5"), revenue_mom=Decimal("3.2"), source="mops",
        ),
        TwInstitutionalDaily(
            market="TW", symbol="2330", ts=date(2026, 7, 15),
            fini_buy=1200, fini_sell=200, source="twse",
        ),
    ])
    await db_session.commit()

    assert await sync_thesis_events(db_session, now=now) == 4
    assert await sync_thesis_events(db_session, now=now) == 0

    events = list((await db_session.scalars(
        select(ThesisEvent).where(ThesisEvent.thesis_id == thesis.id)
    )).all())
    assert {event.event_type for event in events} == {
        "announcement", "news", "revenue", "institutional",
    }
    assert {event.user_id for event in events} == {user.id}
    institutional = next(event for event in events if event.event_type == "institutional")
    assert institutional.details["foreign_net"] == 1000


@pytest.mark.asyncio
async def test_sync_ignores_closed_theses(db_session: AsyncSession):
    user = User(email="thesis-sync-closed@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    db_session.add(InvestmentThesis(
        user_id=user.id, market="US", symbol="AAPL", title="Closed", core_case="case", status="closed",
    ))
    db_session.add(NewsArticle(
        market="US", symbol="AAPL", published_at=datetime.now(UTC), title="News",
        link="https://example.test/closed", source="archive", dedup_hash="c" * 64,
    ))
    await db_session.commit()

    assert await sync_thesis_events(db_session) == 0


@pytest.mark.asyncio
async def test_sync_triggers_structured_watch_conditions_idempotently(db_session: AsyncSession):
    now = datetime(2026, 7, 15, 8, tzinfo=UTC)
    user = User(email="thesis-watch@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    thesis = InvestmentThesis(
        user_id=user.id,
        market="TW",
        symbol="2330",
        title="Growth guardrails",
        core_case="Revenue growth remains healthy",
        watch_conditions=[
            {
                "id": "revenue-growth-floor",
                "label": "Revenue growth below 10%",
                "metric": "revenue_yoy_pct",
                "operator": "lt",
                "threshold": 10,
            },
            {
                "id": "foreign-selling",
                "label": "Foreign net selling",
                "metric": "foreign_net",
                "operator": "lt",
                "threshold": 0,
            },
        ],
    )
    db_session.add(thesis)
    db_session.add_all([
        TwRevenueMonthly(
            market="TW", symbol="2330", ts=date(2026, 7, 1), revenue=1000,
            revenue_yoy=Decimal("7.5"), revenue_mom=Decimal("1.2"), source="mops",
        ),
        TwInstitutionalDaily(
            market="TW", symbol="2330", ts=date(2026, 7, 15),
            fini_buy=100, fini_sell=300, source="twse",
        ),
    ])
    await db_session.commit()

    assert await sync_thesis_events(db_session, now=now) == 4
    assert await sync_thesis_events(db_session, now=now) == 0

    triggers = list((await db_session.scalars(select(ThesisEvent).where(
        ThesisEvent.thesis_id == thesis.id,
        ThesisEvent.event_type == "watch_condition_triggered",
    ))).all())
    assert len(triggers) == 2
    assert {event.details["observed_value"] for event in triggers} == {7.5, -200.0}
    assert all(event.source == "thesis_watch_engine" for event in triggers)


@pytest.mark.asyncio
async def test_sync_ignores_unmatched_and_malformed_watch_conditions(db_session: AsyncSession):
    now = datetime(2026, 7, 15, 8, tzinfo=UTC)
    user = User(email="thesis-watch-safe@test.com", hashed_password="x", role=UserRole.viewer)
    db_session.add(user)
    await db_session.flush()
    thesis = InvestmentThesis(
        user_id=user.id,
        market="TW",
        symbol="2330",
        title="Safe evaluator",
        core_case="Malformed legacy JSON must not stop the scheduler",
        watch_conditions=[
            {"metric": "unknown", "operator": "lt", "threshold": 1},
            {"metric": "revenue_yoy_pct", "operator": "gt", "threshold": 50},
            "legacy-free-text-condition",
        ],
    )
    db_session.add(thesis)
    db_session.add(TwRevenueMonthly(
        market="TW", symbol="2330", ts=date(2026, 7, 1), revenue=1000,
        revenue_yoy=Decimal("20"), source="mops",
    ))
    await db_session.commit()

    assert await sync_thesis_events(db_session, now=now) == 1
    trigger_count = len(list((await db_session.scalars(select(ThesisEvent).where(
        ThesisEvent.thesis_id == thesis.id,
        ThesisEvent.event_type == "watch_condition_triggered",
    ))).all()))
    assert trigger_count == 0
