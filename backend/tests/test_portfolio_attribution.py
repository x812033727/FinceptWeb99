from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _portfolio(client: AsyncClient, token: str, name: str = "Attribution") -> str:
    response = await client.post(
        "/api/portfolio", headers=_auth(token), json={"name": name, "currency": "USD"},
    )
    return response.json()["id"]


async def _transaction(
    client: AsyncClient, token: str, portfolio_id: str, *,
    tx_type: str, quantity: float, price: float, tx_date: date,
) -> None:
    with patch(
        "services.portfolio_service.us_quote", new_callable=AsyncMock,
        return_value={"price": price, "currency": "USD"},
    ):
        response = await client.post(
            f"/api/portfolio/{portfolio_id}/transaction", headers=_auth(token),
            json={
                "symbol": "AAPL", "market": "US", "tx_type": tx_type,
                "quantity": quantity, "price": price, "fx_rate": 1.0,
                "tx_date": tx_date.isoformat(),
            },
        )
    assert response.status_code == 201, response.text


async def _cash_deposit(
    client: AsyncClient, token: str, portfolio_id: str, *,
    amount: float, occurred_on: date,
) -> None:
    response = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries", headers=_auth(token),
        json={
            "currency": "USD", "amount": amount, "entry_type": "deposit",
            "occurred_on": occurred_on.isoformat(),
        },
    )
    assert response.status_code == 201, response.text


def _history(start: date, end: date):
    async def history(symbol: str, period: str = "3mo", interval: str = "1d"):
        start_price, end_price = (100.0, 110.0) if symbol == "SPY" else (100.0, 120.0)
        return [
            {"date": start.isoformat(), "close": start_price},
            {"date": end.isoformat(), "close": end_price},
        ]
    return patch(
        "services.portfolio_attribution_service.us_history",
        new=AsyncMock(side_effect=history),
    )


@pytest.mark.asyncio
async def test_attribution_modified_dietz_reconciles_flows_and_benchmark(client: AsyncClient):
    token = await _token(client, "attribution@test.com")
    portfolio_id = await _portfolio(client, token)
    end = date.today()
    start = end - timedelta(days=90)
    await _cash_deposit(
        client, token, portfolio_id, amount=1000,
        occurred_on=start - timedelta(days=11),
    )
    await _transaction(
        client, token, portfolio_id, tx_type="buy", quantity=10, price=100,
        tx_date=start - timedelta(days=10),
    )
    await _transaction(
        client, token, portfolio_id, tx_type="buy", quantity=5, price=110,
        tx_date=start + timedelta(days=45),
    )
    await _cash_deposit(
        client, token, portfolio_id, amount=550,
        occurred_on=start + timedelta(days=45),
    )
    await _transaction(
        client, token, portfolio_id, tx_type="dividend", quantity=1, price=20,
        tx_date=start + timedelta(days=45),
    )

    with _history(start, end):
        response = await client.get(
            f"/api/portfolio/{portfolio_id}/attribution?days=90", headers=_auth(token),
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["methodology_version"] == "modified-dietz-cash-ledger-v2"
    assert data["benchmark"] == "SPY"
    assert data["benchmark_return_pct"] == pytest.approx(10.0)
    assert len(data["positions"]) == 2
    row = next(item for item in data["positions"] if item["symbol"] == "AAPL")
    cash = next(item for item in data["positions"] if item["symbol"] == "CASH:USD")
    assert row["start_value"] == 1000.0
    assert row["end_value"] == 1800.0
    assert row["net_cash_flow"] == 530.0  # 550 buy - 20 dividend income
    assert row["pnl_after_flows"] == 270.0
    assert cash["start_value"] == 0
    assert cash["end_value"] == 20
    assert cash["pnl_after_flows"] == 0
    assert row["start_weight_pct"] == pytest.approx(99.2157, abs=.0001)
    assert row["contribution_pct"] == pytest.approx(data["portfolio_return_pct"])
    us_market = next(item for item in data["markets"] if item["market"] == "US")
    cash_market = next(item for item in data["markets"] if item["market"] == "CASH")
    assert us_market["contribution_pct"] == data["portfolio_return_pct"]
    assert us_market["pnl_after_flows"] == 270
    assert cash_market["pnl_after_flows"] == 0
    assert data["active_return_pct"] == pytest.approx(
        data["portfolio_return_pct"] - data["benchmark_return_pct"], abs=1e-4,
    )


@pytest.mark.asyncio
async def test_attribution_owner_scope_and_days_validation(client: AsyncClient):
    owner = await _token(client, "attr-owner@test.com")
    stranger = await _token(client, "attr-stranger@test.com")
    portfolio_id = await _portfolio(client, owner, "Private attribution")

    response = await client.get(
        f"/api/portfolio/{portfolio_id}/attribution", headers=_auth(stranger),
    )
    assert response.status_code == 404

    response = await client.get(
        f"/api/portfolio/{portfolio_id}/attribution?days=42", headers=_auth(owner),
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_attribution_empty_portfolio_is_explicit(client: AsyncClient):
    token = await _token(client, "attr-empty@test.com")
    portfolio_id = await _portfolio(client, token, "Empty attribution")
    response = await client.get(
        f"/api/portfolio/{portfolio_id}/attribution", headers=_auth(token),
    )
    assert response.status_code == 200
    assert response.json()["empty"] is True
    assert response.json()["positions"] == []
    assert response.json()["markets"] == []
