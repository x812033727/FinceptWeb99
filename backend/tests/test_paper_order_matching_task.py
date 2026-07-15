from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select

from models.paper_trading import PaperOrder
from models.portfolio import Portfolio
from services import paper_trading_service
from tasks import paper_order_matching as task


async def _account(client, db_session, email: str) -> tuple[str, str]:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    login = await client.post("/api/auth/login", json={"email": email, "password": "Test1234!"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.post(
        "/api/portfolio", json={"name": "Automation", "currency": "USD"}, headers=headers
    )
    portfolio_id = response.json()["id"]
    await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries",
        json={
            "currency": "USD",
            "amount": 10_000,
            "entry_type": "deposit",
            "idempotency_key": f"opening-{email}",
        },
        headers=headers,
    )
    portfolio = await db_session.get(Portfolio, UUID(portfolio_id))
    return portfolio_id, str(portfolio.user_id)


async def _order(db, portfolio_id: str, user_id: str, key: str, limit: float):
    return await paper_trading_service.submit_order(
        portfolio_id=portfolio_id,
        user_id=user_id,
        symbol="AAPL",
        market="US",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=limit,
        reference_price=None,
        fee_bps=0,
        idempotency_key=key,
        notes=None,
        time_in_force="gtc",
        db=db,
    )


@pytest.mark.asyncio
async def test_matcher_reuses_quotes_and_isolates_order_failures(client, db_session, monkeypatch):
    portfolio_id, user_id = await _account(client, db_session, "paper-automation@example.com")
    triggered = await _order(db_session, portfolio_id, user_id, "auto-triggered", 100)
    untouched = await _order(db_session, portfolio_id, user_id, "auto-untriggered", 90)
    broken = await _order(db_session, portfolio_id, user_id, "auto-broken", 100)
    await db_session.commit()

    quote_calls = 0

    async def quote_stub(market, symbol):
        nonlocal quote_calls
        quote_calls += 1
        assert (market, symbol) == ("US", "AAPL")
        return {"ask": 99}

    original_match = task.paper_matching_service.match_order

    async def isolated_failure(**kwargs):
        if kwargs["order_id"] == str(broken.id):
            raise RuntimeError("one bad order")
        return await original_match(**kwargs)

    monkeypatch.setattr(task.paper_matching_service, "get_market_quote", quote_stub)
    monkeypatch.setattr(task.paper_matching_service, "match_order", isolated_failure)
    stats = await task.match_open_paper_orders(now=datetime(2026, 7, 15, 14, 0, tzinfo=UTC))

    assert stats.scanned == 3
    assert stats.matched == 1
    assert stats.untriggered == 1
    assert stats.failed == 1
    assert quote_calls == 1
    async with task.AsyncSessionLocal() as verify:
        rows = {
            row.id: row
            for row in (
                await verify.scalars(
                    select(PaperOrder).where(
                        PaperOrder.id.in_(
                            [
                                triggered.id,
                                untouched.id,
                                broken.id,
                            ]
                        )
                    )
                )
            ).all()
        }
    assert rows[triggered.id].status == "filled"
    assert rows[untouched.id].status == "pending"
    assert rows[broken.id].status == "pending"


@pytest.mark.asyncio
async def test_matcher_expires_day_orders_skips_closed_markets_and_honors_lock(
    client, db_session, monkeypatch
):
    portfolio_id, user_id = await _account(
        client, db_session, "paper-expiry-automation@example.com"
    )
    expired = await _order(db_session, portfolio_id, user_id, "auto-expired", 100)
    expired.time_in_force = "day"
    expired.expires_at = datetime(2026, 7, 15, 20, 0, tzinfo=UTC) - timedelta(minutes=1)
    await _order(db_session, portfolio_id, user_id, "auto-closed", 100)
    await db_session.commit()

    quote = AsyncMock(return_value={"ask": 99})
    monkeypatch.setattr(task.paper_matching_service, "get_market_quote", quote)
    stats = await task.match_open_paper_orders(now=datetime(2026, 7, 18, 14, 0, tzinfo=UTC))
    assert stats.expired == 1
    assert stats.closed == 1
    quote.assert_not_awaited()

    monkeypatch.setattr(task, "acquire_lock", AsyncMock(return_value=False))
    locked = await task.match_open_paper_orders()
    assert locked.lock_held is True
    assert locked.scanned == 0


@pytest.mark.asyncio
async def test_matcher_does_not_fetch_stale_quotes_on_exchange_holidays(
    client, db_session, monkeypatch
):
    portfolio_id, user_id = await _account(
        client, db_session, "paper-holiday-automation@example.com"
    )
    await _order(db_session, portfolio_id, user_id, "auto-us-holiday", 100)
    await db_session.commit()

    quote = AsyncMock(return_value={"ask": 99})
    monkeypatch.setattr(task.paper_matching_service, "get_market_quote", quote)
    stats = await task.match_open_paper_orders(now=datetime(2026, 1, 19, 15, 0, tzinfo=UTC))

    assert stats.scanned == 1
    assert stats.closed == 1
    assert stats.matched == 0
    quote.assert_not_awaited()


def test_scheduler_registers_automatic_matcher_at_fifteen_seconds():
    from tasks.scheduler import scheduler, setup_jobs

    with patch.object(scheduler, "add_job") as add_job:
        setup_jobs()
    call = next(
        item for item in add_job.call_args_list if item.kwargs.get("id") == "paper_order_matching"
    )
    assert call.kwargs["trigger"].interval.total_seconds() == 15
    assert call.kwargs["max_instances"] == 1
    assert call.kwargs["coalesce"] is True
