from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.portfolio import Portfolio
from services import paper_performance_service


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "Test1234!"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _portfolio(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/api/portfolio",
        json={"name": "Performance Account", "currency": "USD"},
        headers=headers,
    )
    portfolio_id = response.json()["id"]
    deposited = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries",
        json={
            "currency": "USD",
            "amount": 10_000,
            "entry_type": "deposit",
            "idempotency_key": "performance-opening-cash",
        },
        headers=headers,
    )
    assert deposited.status_code == 201
    return portfolio_id


async def _fill(
    client: AsyncClient,
    headers: dict[str, str],
    portfolio_id: str,
    *,
    key: str,
    side: str,
    quantity: float,
    price: float,
    fee_bps: float = 0,
) -> dict:
    orders = f"/api/portfolio/{portfolio_id}/paper-orders"
    order = await client.post(
        orders,
        json={
            "symbol": "AAPL",
            "market": "US",
            "side": side,
            "order_type": "limit",
            "time_in_force": "gtc",
            "quantity": quantity,
            "limit_price": price,
            "fee_bps": fee_bps,
            "idempotency_key": f"{key}-order",
        },
        headers=headers,
    )
    assert order.status_code == 201
    fill = await client.post(
        f"{orders}/{order.json()['id']}/fills",
        json={
            "quantity": quantity,
            "price": price,
            "idempotency_key": f"{key}-fill",
        },
        headers=headers,
    )
    assert fill.status_code == 201
    return fill.json()


@pytest.mark.asyncio
async def test_empty_performance_is_owner_scoped(client: AsyncClient):
    owner = await _login(client, "performance-owner@example.com")
    stranger = await _login(client, "performance-stranger@example.com")
    portfolio_id = await _portfolio(client, owner)
    endpoint = f"/api/portfolio/{portfolio_id}/paper-performance"

    response = await client.get(endpoint, headers=owner)

    assert response.status_code == 200
    assert response.json()["total_fill_count"] == 0
    assert response.json()["curve"] == []
    assert {row["currency"] for row in response.json()["summaries"]} == {"USD", "TWD"}
    assert (await client.get(endpoint, headers=stranger)).status_code == 404


@pytest.mark.asyncio
async def test_performance_aggregates_exit_orders_and_drawdown(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _login(client, "performance-metrics@example.com")
    portfolio_id = await _portfolio(client, headers)
    await _fill(
        client,
        headers,
        portfolio_id,
        key="performance-buy",
        side="buy",
        quantity=3,
        price=100,
        fee_bps=100,
    )
    await _fill(
        client,
        headers,
        portfolio_id,
        key="performance-win",
        side="sell",
        quantity=1,
        price=120,
        fee_bps=100,
    )
    await _fill(
        client,
        headers,
        portfolio_id,
        key="performance-loss",
        side="sell",
        quantity=1,
        price=80,
    )

    portfolio = await db_session.scalar(
        select(Portfolio).where(Portfolio.id == UUID(portfolio_id))
    )
    assert portfolio is not None
    body = await paper_performance_service.performance(
        portfolio_id=portfolio_id,
        user_id=str(portfolio.user_id),
        db=db_session,
        fill_limit=2000,
    )
    usd = next(row for row in body["summaries"] if row["currency"] == "USD")

    assert body["total_fill_count"] == body["window_fill_count"] == 3
    assert body["truncated"] is False
    assert [point["cumulative_realized_pnl"] for point in body["curve"]] == pytest.approx(
        [-3, 15.8, -4.2]
    )
    assert usd["exit_order_count"] == 2
    assert usd["winning_exit_orders"] == usd["losing_exit_orders"] == 1
    assert usd["win_rate_pct"] == 50
    assert usd["profit_factor"] == pytest.approx(0.94)
    assert usd["best_exit_pnl"] == pytest.approx(18.8)
    assert usd["worst_exit_pnl"] == pytest.approx(-20)
    assert usd["max_drawdown"] == pytest.approx(-20)
    assert usd["total_realized_pnl"] == pytest.approx(-4.2)
    assert usd["total_fees"] == pytest.approx(4.2)

    window = await paper_performance_service.performance(
        portfolio_id=portfolio_id,
        user_id=str(portfolio.user_id),
        db=db_session,
        fill_limit=2,
    )
    assert window["window_fill_count"] == 2
    assert window["total_fill_count"] == 3
    assert window["truncated"] is True

    with pytest.raises(ValueError, match="Portfolio not found"):
        await paper_performance_service.performance(
            portfolio_id=portfolio_id,
            user_id=str(uuid4()),
            db=db_session,
            fill_limit=2,
        )
