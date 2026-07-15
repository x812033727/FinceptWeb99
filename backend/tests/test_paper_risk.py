from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperOrder, PaperRiskPolicy
from models.portfolio import Holding, Market, Portfolio
from services import paper_risk_service


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Test1234!"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _portfolio(
    client: AsyncClient, headers: dict[str, str], *, usd: float = 10_000, twd: float = 0
) -> str:
    response = await client.post(
        "/api/portfolio",
        json={"name": "Risk Account", "currency": "USD"},
        headers=headers,
    )
    portfolio_id = response.json()["id"]
    for currency, amount in (("USD", usd), ("TWD", twd)):
        if amount:
            result = await client.post(
                f"/api/portfolio/{portfolio_id}/cash-entries",
                json={
                    "currency": currency,
                    "amount": amount,
                    "entry_type": "deposit",
                    "idempotency_key": f"risk-opening-{currency.lower()}",
                },
                headers=headers,
            )
            assert result.status_code == 201
    return portfolio_id


def _order(key: str, **overrides) -> dict:
    payload = {
        "symbol": "AAPL",
        "market": "US",
        "side": "buy",
        "order_type": "limit",
        "quantity": 1,
        "limit_price": 100,
        "time_in_force": "gtc",
        "idempotency_key": key,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_policy_defaults_validation_and_owner_scope(client: AsyncClient):
    owner = await _login(client, "risk-owner@example.com")
    stranger = await _login(client, "risk-stranger@example.com")
    portfolio_id = await _portfolio(client, owner)
    endpoint = f"/api/portfolio/{portfolio_id}/paper-risk-policy"

    default = await client.get(endpoint, headers=owner)
    assert default.status_code == 200
    assert default.json()["configured"] is False
    assert default.json()["trading_enabled"] is True
    assert default.json()["daily_realized_pnl_usd"] == 0
    assert (await client.get(endpoint, headers=stranger)).status_code == 404
    assert (
        await client.put(endpoint, json={"max_open_orders": 2}, headers=stranger)
    ).status_code == 404
    invalid = await client.put(
        endpoint,
        json={"max_symbol_concentration_pct": 101},
        headers=owner,
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_submission_caps_order_position_concentration_and_open_orders(
    client: AsyncClient,
):
    headers = await _login(client, "risk-caps@example.com")
    portfolio_id = await _portfolio(client, headers, usd=1_000)
    policy = f"/api/portfolio/{portfolio_id}/paper-risk-policy"
    orders = f"/api/portfolio/{portfolio_id}/paper-orders"

    await client.put(policy, json={"max_order_notional_usd": 200}, headers=headers)
    too_large = await client.post(
        orders, json=_order("risk-order-too-large", quantity=3), headers=headers
    )
    assert too_large.status_code == 409
    assert "order notional" in too_large.json()["detail"]

    await client.put(
        policy,
        json={
            "max_order_notional_usd": 500,
            "max_symbol_concentration_pct": 25,
        },
        headers=headers,
    )
    concentrated = await client.post(
        orders, json=_order("risk-order-concentrated", quantity=3), headers=headers
    )
    assert concentrated.status_code == 409
    assert "concentration" in concentrated.json()["detail"]

    await client.put(
        policy,
        json={"max_position_notional_usd": 150},
        headers=headers,
    )
    position = await client.post(
        orders, json=_order("risk-order-position", quantity=2), headers=headers
    )
    assert position.status_code == 409
    assert "position limit" in position.json()["detail"]

    await client.put(policy, json={"max_open_orders": 1}, headers=headers)
    accepted = await client.post(orders, json=_order("risk-order-open-first"), headers=headers)
    assert accepted.status_code == 201
    open_limit = await client.post(
        orders,
        json=_order("risk-order-open-second", symbol="MSFT"),
        headers=headers,
    )
    assert open_limit.status_code == 409
    assert "open paper order limit" in open_limit.json()["detail"]


@pytest.mark.asyncio
async def test_twd_order_limit_uses_settlement_currency(client: AsyncClient):
    headers = await _login(client, "risk-twd@example.com")
    portfolio_id = await _portfolio(client, headers, usd=0, twd=100_000)
    await client.put(
        f"/api/portfolio/{portfolio_id}/paper-risk-policy",
        json={"max_order_notional_twd": 10_000},
        headers=headers,
    )
    response = await client.post(
        f"/api/portfolio/{portfolio_id}/paper-orders",
        json=_order(
            "risk-order-twd",
            symbol="2330",
            market="TW",
            quantity=20,
            limit_price=600,
        ),
        headers=headers,
    )
    assert response.status_code == 409
    assert "12000.000000 TWD" in response.json()["detail"]


@pytest.mark.asyncio
async def test_kill_switch_cancels_open_orders_and_preserves_idempotent_retry(
    client: AsyncClient,
):
    headers = await _login(client, "risk-kill-switch@example.com")
    portfolio_id = await _portfolio(client, headers)
    orders = f"/api/portfolio/{portfolio_id}/paper-orders"
    first_payload = _order("risk-kill-first")
    first = await client.post(orders, json=first_payload, headers=headers)
    second = await client.post(
        orders, json=_order("risk-kill-second", symbol="MSFT"), headers=headers
    )
    assert first.status_code == second.status_code == 201

    policy = await client.put(
        f"/api/portfolio/{portfolio_id}/paper-risk-policy",
        json={"trading_enabled": False},
        headers=headers,
    )
    assert policy.status_code == 200
    assert policy.json()["cancelled_open_orders"] == 2
    assert policy.json()["trading_enabled"] is False
    rows = (await client.get(orders, headers=headers)).json()
    assert {row["status"] for row in rows} == {"cancelled"}

    blocked = await client.post(
        orders, json=_order("risk-kill-blocked", symbol="NVDA"), headers=headers
    )
    assert blocked.status_code == 409
    assert "kill switch" in blocked.json()["detail"]
    retry = await client.post(orders, json=first_payload, headers=headers)
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert retry.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_realized_daily_loss_blocks_subsequent_orders(client: AsyncClient):
    headers = await _login(client, "risk-daily-loss@example.com")
    portfolio_id = await _portfolio(client, headers)
    transaction = await client.post(
        f"/api/portfolio/{portfolio_id}/transaction",
        json={
            "symbol": "AAPL",
            "market": "US",
            "tx_type": "buy",
            "quantity": 2,
            "price": 100,
            "fx_rate": 1,
            "tx_date": "2026-07-15",
        },
        headers=headers,
    )
    assert transaction.status_code == 201
    policy_endpoint = f"/api/portfolio/{portfolio_id}/paper-risk-policy"
    await client.put(
        policy_endpoint,
        json={"max_daily_loss_usd": 50},
        headers=headers,
    )
    orders = f"/api/portfolio/{portfolio_id}/paper-orders"
    sell = await client.post(
        orders,
        json=_order(
            "risk-loss-sell",
            side="sell",
            quantity=1,
            limit_price=40,
        ),
        headers=headers,
    )
    assert sell.status_code == 201
    fill = await client.post(
        f"{orders}/{sell.json()['id']}/fills",
        json={
            "quantity": 1,
            "price": 40,
            "idempotency_key": "risk-loss-fill",
        },
        headers=headers,
    )
    assert fill.status_code == 201
    assert fill.json()["currency"] == "USD"
    assert fill.json()["realized_pnl"] == pytest.approx(-60)

    state = (await client.get(policy_endpoint, headers=headers)).json()
    assert state["daily_realized_pnl_usd"] == pytest.approx(-60)
    blocked = await client.post(
        orders, json=_order("risk-loss-blocked", symbol="MSFT"), headers=headers
    )
    assert blocked.status_code == 409
    assert "daily USD realized loss limit reached" in blocked.json()["detail"]


@pytest.mark.asyncio
async def test_fill_rechecks_policy_changed_after_submission(client: AsyncClient):
    headers = await _login(client, "risk-fill-recheck@example.com")
    portfolio_id = await _portfolio(client, headers)
    orders = f"/api/portfolio/{portfolio_id}/paper-orders"
    order = await client.post(
        orders, json=_order("risk-fill-recheck-order", quantity=2), headers=headers
    )
    assert order.status_code == 201
    await client.put(
        f"/api/portfolio/{portfolio_id}/paper-risk-policy",
        json={"max_position_notional_usd": 150},
        headers=headers,
    )
    fill = await client.post(
        f"{orders}/{order.json()['id']}/fills",
        json={
            "quantity": 2,
            "price": 100,
            "idempotency_key": "risk-fill-recheck-fill",
        },
        headers=headers,
    )
    assert fill.status_code == 409
    assert "position limit" in fill.json()["detail"]
    persisted = await client.get(f"{orders}/{order.json()['id']}", headers=headers)
    assert persisted.json()["status"] == "pending"


def _paper_order(*, market: str = "US", side: str = "buy", **overrides) -> PaperOrder:
    values = {
        "id": uuid4(),
        "portfolio_id": uuid4(),
        "symbol": "AAPL",
        "market": market,
        "side": side,
        "order_type": "limit",
        "time_in_force": "gtc",
        "quantity": 10,
        "filled_quantity": 2,
        "limit_price": 100,
        "reservation_price": 100,
        "fee_bps": 0,
        "status": "pending",
        "idempotency_key": str(uuid4()),
    }
    values.update(overrides)
    return PaperOrder(**values)


def test_pending_buy_exposure_groups_symbols_and_adjusts_current_fill():
    current = _paper_order()
    same_symbol = _paper_order(quantity=1, filled_quantity=0, reservation_price=50)
    ignored = [
        _paper_order(market="TW"),
        _paper_order(side="sell"),
    ]

    gross, by_symbol = paper_risk_service._pending_buy_exposure(
        [current, same_symbol, *ignored],
        "USD",
        current_order_id=current.id,
        current_fill_quantity=3,
    )

    assert gross == pytest.approx(550)
    assert by_symbol == {("US", "AAPL"): pytest.approx(550)}


@pytest.mark.asyncio
async def test_cost_exposure_combines_cash_and_average_cost_holdings(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _login(client, "risk-cost-exposure@example.com")
    portfolio_id = await _portfolio(client, headers, usd=1_000)
    db_session.add_all(
        [
            Holding(
                portfolio_id=UUID(portfolio_id),
                symbol="AAPL",
                market=Market.US,
                quantity=2,
                avg_cost=100,
                cost_currency="USD",
            ),
            Holding(
                portfolio_id=UUID(portfolio_id),
                symbol="AAPL",
                market=Market.US,
                quantity=1,
                avg_cost=50,
                cost_currency="USD",
            ),
            Holding(
                portfolio_id=UUID(portfolio_id),
                symbol="2330",
                market=Market.TW,
                quantity=1,
                avg_cost=600,
                cost_currency="TWD",
            ),
        ]
    )
    await db_session.commit()

    capital, by_symbol = await paper_risk_service._cost_exposure(
        portfolio_id, "USD", db_session
    )

    assert capital == pytest.approx(1_250)
    assert by_symbol == {("US", "AAPL"): pytest.approx(250)}


@pytest.mark.asyncio
async def test_service_allows_unconfigured_policy_and_sell_paths(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _login(client, "risk-service-paths@example.com")
    portfolio_id = await _portfolio(client, headers, usd=1_000)
    sell = _paper_order(portfolio_id=UUID(portfolio_id), side="sell")

    await paper_risk_service.enforce_submission(
        portfolio_id=portfolio_id,
        market="US",
        symbol="AAPL",
        side="sell",
        quantity=1,
        reservation_price=100,
        open_orders=[],
        db=db_session,
    )
    await paper_risk_service.enforce_fill(
        portfolio_id=portfolio_id,
        order=sell,
        quantity=1,
        price=100,
        open_orders=[],
        db=db_session,
    )

    db_session.add(PaperRiskPolicy(portfolio_id=UUID(portfolio_id), trading_enabled=True))
    await db_session.commit()
    await paper_risk_service.enforce_submission(
        portfolio_id=portfolio_id,
        market="US",
        symbol="AAPL",
        side="sell",
        quantity=1,
        reservation_price=100,
        open_orders=[],
        db=db_session,
    )
    await paper_risk_service.enforce_fill(
        portfolio_id=portfolio_id,
        order=sell,
        quantity=1,
        price=100,
        open_orders=[],
        db=db_session,
    )


@pytest.mark.asyncio
async def test_zero_capital_concentration_and_disabled_policy_are_blocked(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _login(client, "risk-zero-capital@example.com")
    portfolio_id = await _portfolio(client, headers, usd=0)
    policy = PaperRiskPolicy(
        portfolio_id=portfolio_id,
        trading_enabled=True,
        max_symbol_concentration_pct=10,
    )

    with pytest.raises(paper_risk_service.PaperRiskViolation, match="concentration"):
        await paper_risk_service._enforce_buy_exposure(
            policy=policy,
            portfolio_id=portfolio_id,
            market="US",
            symbol="AAPL",
            additional_notional=1,
            open_orders=[],
            db=db_session,
        )

    policy.trading_enabled = False
    with pytest.raises(paper_risk_service.PaperRiskViolation, match="kill switch"):
        await paper_risk_service._enforce_common(policy, portfolio_id, "USD", db_session)


@pytest.mark.asyncio
async def test_service_policy_lifecycle_reports_limits_and_cancelled_orders(
    client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _login(client, "risk-policy-lifecycle@example.com")
    portfolio_id = await _portfolio(client, headers, usd=1_000)
    created = await client.post(
        f"/api/portfolio/{portfolio_id}/paper-orders",
        json=_order("risk-policy-lifecycle-order"),
        headers=headers,
    )
    assert created.status_code == 201
    portfolio = await db_session.scalar(
        select(Portfolio).where(Portfolio.id == UUID(portfolio_id))
    )
    assert portfolio is not None

    disabled = await paper_risk_service.update_policy(
        portfolio_id=portfolio_id,
        user_id=str(portfolio.user_id),
        trading_enabled=False,
        db=db_session,
        max_order_notional_usd=500,
        max_order_notional_twd=10_000,
        max_position_notional_usd=750,
        max_position_notional_twd=20_000,
        max_daily_loss_usd=100,
        max_daily_loss_twd=3_000,
        max_open_orders=3,
        max_symbol_concentration_pct=25,
    )

    assert disabled["configured"] is True
    assert disabled["trading_enabled"] is False
    assert disabled["cancelled_open_orders"] == 1
    assert disabled["max_open_orders"] == 3
    assert disabled["max_order_notional_usd"] == pytest.approx(500)
    assert disabled["daily_realized_pnl_usd"] == 0
    assert disabled["daily_realized_pnl_twd"] == 0

    enabled = await paper_risk_service.update_policy(
        portfolio_id=portfolio_id,
        user_id=str(portfolio.user_id),
        trading_enabled=True,
        db=db_session,
    )
    assert enabled["trading_enabled"] is True
    assert enabled["cancelled_open_orders"] == 0

    with pytest.raises(ValueError, match="Portfolio not found"):
        await paper_risk_service.get_policy_state(
            portfolio_id=str(uuid4()),
            user_id=str(portfolio.user_id),
            db=db_session,
        )
