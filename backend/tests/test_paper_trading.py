import pytest
from httpx import AsyncClient


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/api/auth/register",
        json={"email": email, "password": "Test1234!"},
    )
    response = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "Test1234!"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _portfolio(
    client: AsyncClient, headers: dict[str, str], *, cash: float = 10_000,
) -> str:
    response = await client.post(
        "/api/portfolio",
        json={"name": "Paper Account", "currency": "USD"},
        headers=headers,
    )
    portfolio_id = response.json()["id"]
    await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries",
        json={
            "currency": "USD",
            "amount": cash,
            "entry_type": "deposit",
            "idempotency_key": "paper-opening-cash",
        },
        headers=headers,
    )
    return portfolio_id


def _buy_payload(**overrides):
    payload = {
        "symbol": "AAPL",
        "market": "US",
        "side": "buy",
        "order_type": "limit",
        "quantity": 10,
        "limit_price": 100,
        "fee_bps": 100,
        "idempotency_key": "paper-buy-aapl-001",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_buy_order_is_idempotent_reserves_cash_and_supports_partial_fill_cancel(
    client: AsyncClient,
):
    headers = await _login(client, "paper-buy@example.com")
    portfolio_id = await _portfolio(client, headers)
    endpoint = f"/api/portfolio/{portfolio_id}/paper-orders"

    created = await client.post(endpoint, json=_buy_payload(), headers=headers)
    duplicate = await client.post(endpoint, json=_buy_payload(), headers=headers)
    assert created.status_code == duplicate.status_code == 201
    assert created.json()["id"] == duplicate.json()["id"]
    order_id = created.json()["id"]

    conflicting_retry = await client.post(
        endpoint,
        json=_buy_payload(quantity=11),
        headers=headers,
    )
    assert conflicting_retry.status_code == 409

    reserved_too_much = await client.post(
        endpoint,
        json=_buy_payload(
            quantity=90,
            idempotency_key="paper-buy-aapl-002",
        ),
        headers=headers,
    )
    assert reserved_too_much.status_code == 409
    assert "insufficient USD cash" in reserved_too_much.json()["detail"]

    fill_payload = {
        "quantity": 4,
        "price": 99,
        "idempotency_key": "paper-fill-aapl-001",
        "filled_at": "2026-07-15T10:00:00Z",
    }
    fill = await client.post(
        f"{endpoint}/{order_id}/fills", json=fill_payload, headers=headers,
    )
    duplicate_fill = await client.post(
        f"{endpoint}/{order_id}/fills", json=fill_payload, headers=headers,
    )
    assert fill.status_code == duplicate_fill.status_code == 201
    assert fill.json()["id"] == duplicate_fill.json()["id"]
    assert fill.json()["fee"] == pytest.approx(3.96)
    fills = (
        await client.get(f"{endpoint}/{order_id}/fills", headers=headers)
    ).json()
    assert [row["id"] for row in fills] == [fill.json()["id"]]

    order = (await client.get(f"{endpoint}/{order_id}", headers=headers)).json()
    assert order["status"] == "partially_filled"
    assert order["filled_quantity"] == 4
    assert order["average_fill_price"] == 99
    portfolio = (
        await client.get(f"/api/portfolio/{portfolio_id}", headers=headers)
    ).json()
    assert portfolio["holdings"][0]["quantity"] == 4
    transaction_id = fill.json()["transaction_id"]
    assert (
        await client.patch(
            f"/api/portfolio/{portfolio_id}/transactions/{transaction_id}",
            json={"quantity": 5},
            headers=headers,
        )
    ).status_code == 400
    assert (
        await client.delete(
            f"/api/portfolio/{portfolio_id}/transactions/{transaction_id}",
            headers=headers,
        )
    ).status_code == 400
    cash = (
        await client.get(f"/api/portfolio/{portfolio_id}/cash", headers=headers)
    ).json()
    assert cash["balances"]["USD"] == pytest.approx(9_600.04)

    cancelled = await client.post(
        f"{endpoint}/{order_id}/cancel", headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (
        await client.post(
            f"{endpoint}/{order_id}/fills",
            json={
                "quantity": 1,
                "price": 99,
                "idempotency_key": "paper-fill-after-cancel",
            },
            headers=headers,
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_sell_orders_reserve_inventory_and_are_owner_scoped(client: AsyncClient):
    owner = await _login(client, "paper-sell@example.com")
    stranger = await _login(client, "paper-stranger@example.com")
    portfolio_id = await _portfolio(client, owner)
    await client.post(
        f"/api/portfolio/{portfolio_id}/transaction",
        json={
            "symbol": "MSFT",
            "market": "US",
            "tx_type": "buy",
            "quantity": 5,
            "price": 100,
            "fx_rate": 1,
            "tx_date": "2026-07-15",
        },
        headers=owner,
    )
    endpoint = f"/api/portfolio/{portfolio_id}/paper-orders"
    first = await client.post(
        endpoint,
        json={
            "symbol": "MSFT",
            "market": "US",
            "side": "sell",
            "order_type": "limit",
            "quantity": 4,
            "limit_price": 110,
            "idempotency_key": "paper-sell-msft-001",
        },
        headers=owner,
    )
    assert first.status_code == 201
    second = await client.post(
        endpoint,
        json={
            "symbol": "MSFT",
            "market": "US",
            "side": "sell",
            "order_type": "market",
            "quantity": 2,
            "reference_price": 109,
            "idempotency_key": "paper-sell-msft-002",
        },
        headers=owner,
    )
    assert second.status_code == 409
    assert "insufficient inventory" in second.json()["detail"]
    assert (await client.get(endpoint, headers=stranger)).status_code == 404

    below_limit = await client.post(
        f"{endpoint}/{first.json()['id']}/fills",
        json={
            "quantity": 2,
            "price": 109,
            "idempotency_key": "paper-sell-fill-bad",
        },
        headers=owner,
    )
    assert below_limit.status_code == 409
    filled = await client.post(
        f"{endpoint}/{first.json()['id']}/fills",
        json={
            "quantity": 2,
            "price": 111,
            "idempotency_key": "paper-sell-fill-good",
        },
        headers=owner,
    )
    assert filled.status_code == 201
    portfolio = (
        await client.get(f"/api/portfolio/{portfolio_id}", headers=owner)
    ).json()
    assert portfolio["holdings"][0]["quantity"] == 3
