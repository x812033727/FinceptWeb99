from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.portfolio import Portfolio, PortfolioSnapshot


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post("/api/auth/register", json={
        "email": email, "password": "Test1234!",
    })
    response = await client.post("/api/auth/login", json={
        "email": email, "password": "Test1234!",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _portfolio(client: AsyncClient, headers: dict[str, str], currency: str = "TWD") -> str:
    response = await client.post("/api/portfolio", json={
        "name": "Cash Ledger", "currency": currency,
    }, headers=headers)
    return response.json()["id"]


@pytest.mark.asyncio
async def test_manual_cash_entries_are_idempotent_owner_scoped_and_reversible(
    client: AsyncClient,
):
    owner = await _login(client, "cash-owner@example.com")
    stranger = await _login(client, "cash-stranger@example.com")
    portfolio_id = await _portfolio(client, owner)
    payload = {
        "currency": "twd", "amount": 100_000, "entry_type": "deposit",
        "occurred_on": "2026-07-15", "idempotency_key": "deposit-20260715-001",
    }
    created = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries", json=payload, headers=owner,
    )
    duplicate = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries", json=payload, headers=owner,
    )
    assert created.status_code == duplicate.status_code == 201
    assert created.json()["id"] == duplicate.json()["id"]

    balance = await client.get(f"/api/portfolio/{portfolio_id}/cash", headers=owner)
    assert balance.status_code == 200
    assert balance.json()["balances"] == {"TWD": 100_000}
    assert balance.json()["total_cash_base"] == 100_000
    assert (await client.get(
        f"/api/portfolio/{portfolio_id}/cash", headers=stranger,
    )).status_code == 404

    reversed_entry = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries/{created.json()['id']}/reverse",
        json={"notes": "duplicate funding"}, headers=owner,
    )
    assert reversed_entry.status_code == 200
    assert reversed_entry.json()["amount"] == -100_000
    balance = await client.get(f"/api/portfolio/{portfolio_id}/cash", headers=owner)
    assert balance.json()["balances"] == {"TWD": 0}
    second_reverse = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries/{created.json()['id']}/reverse",
        json={}, headers=owner,
    )
    assert second_reverse.status_code == 400
    entries = (await client.get(
        f"/api/portfolio/{portfolio_id}/cash-entries", headers=owner,
    )).json()
    original = next(row for row in entries if row["id"] == created.json()["id"])
    assert original["is_reversed"] is True


@pytest.mark.asyncio
async def test_transactions_append_native_currency_settlements_and_corrections(
    client: AsyncClient,
):
    headers = await _login(client, "cash-trades@example.com")
    portfolio_id = await _portfolio(client, headers, currency="USD")
    await client.post(f"/api/portfolio/{portfolio_id}/cash-entries", json={
        "currency": "USD", "amount": 5_000, "entry_type": "deposit",
        "idempotency_key": "usd-opening-0001",
    }, headers=headers)
    trade = await client.post(f"/api/portfolio/{portfolio_id}/transaction", json={
        "symbol": "AAPL", "market": "US", "tx_type": "buy", "quantity": 10,
        "price": 100, "fx_rate": 1, "tx_date": "2026-07-15",
    }, headers=headers)
    assert trade.status_code == 201
    transactions = (await client.get(
        f"/api/portfolio/{portfolio_id}/transactions", headers=headers,
    )).json()
    tx_id = transactions[0]["id"]
    balance = (await client.get(
        f"/api/portfolio/{portfolio_id}/cash", headers=headers,
    )).json()
    assert balance["balances"]["USD"] == 4_000

    updated = await client.patch(
        f"/api/portfolio/{portfolio_id}/transactions/{tx_id}",
        json={"quantity": 20}, headers=headers,
    )
    assert updated.status_code == 200
    balance = (await client.get(
        f"/api/portfolio/{portfolio_id}/cash", headers=headers,
    )).json()
    assert balance["balances"]["USD"] == 3_000

    deleted = await client.delete(
        f"/api/portfolio/{portfolio_id}/transactions/{tx_id}", headers=headers,
    )
    assert deleted.status_code == 204
    balance = (await client.get(
        f"/api/portfolio/{portfolio_id}/cash", headers=headers,
    )).json()
    assert balance["balances"]["USD"] == 5_000
    entries = (await client.get(
        f"/api/portfolio/{portfolio_id}/cash-entries", headers=headers,
    )).json()
    assert any(row["entry_type"] == "trade_settlement" for row in entries)
    assert sum(row["amount"] for row in entries) == pytest.approx(5_000)


@pytest.mark.asyncio
async def test_rich_snapshot_endpoint_is_owner_scoped(
    client: AsyncClient, db_session: AsyncSession,
):
    headers = await _login(client, "cash-snapshot@example.com")
    stranger = await _login(client, "cash-snapshot-other@example.com")
    portfolio_id = await _portfolio(client, headers)
    portfolio = await db_session.scalar(select(Portfolio).where(
        Portfolio.id == __import__("uuid").UUID(portfolio_id),
    ))
    db_session.add(PortfolioSnapshot(
        portfolio_id=portfolio.id, snapshot_date=date.today(),
        total_value_usd=3_125, base_currency="TWD",
        holdings_value_base=80_000, cash_value_base=20_000,
        total_value_base=100_000,
        positions=[{"symbol": "2330", "quantity": 80}],
        cash_balances={"TWD": 20_000},
        valuation_quality={"status": "complete", "missing_quote_symbols": []},
    ))
    await db_session.flush()
    response = await client.get(
        f"/api/portfolio/{portfolio_id}/snapshots?days=10", headers=headers,
    )
    assert response.status_code == 200
    assert response.json()[0]["positions"][0]["symbol"] == "2330"
    assert response.json()[0]["cash_balances"] == {"TWD": 20_000}
    assert (await client.get(
        f"/api/portfolio/{portfolio_id}/snapshots", headers=stranger,
    )).status_code == 404


@pytest.mark.asyncio
async def test_cash_ledger_rejects_oversells_and_direct_settlement_reversal(
    client: AsyncClient,
):
    headers = await _login(client, "cash-inventory@example.com")
    portfolio_id = await _portfolio(client, headers, currency="USD")
    buy = await client.post(f"/api/portfolio/{portfolio_id}/transaction", json={
        "symbol": "AAPL", "market": "US", "tx_type": "buy", "quantity": 10,
        "price": 100, "fx_rate": 1, "tx_date": "2026-07-14",
    }, headers=headers)
    assert buy.status_code == 201
    oversell = await client.post(f"/api/portfolio/{portfolio_id}/transaction", json={
        "symbol": "AAPL", "market": "US", "tx_type": "sell", "quantity": 11,
        "price": 110, "fx_rate": 1, "tx_date": "2026-07-15",
    }, headers=headers)
    assert oversell.status_code == 400
    assert "available shares" in oversell.json()["detail"]

    entries = (await client.get(
        f"/api/portfolio/{portfolio_id}/cash-entries", headers=headers,
    )).json()
    settlement = next(row for row in entries if row["entry_type"] == "trade_settlement")
    direct_reverse = await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries/{settlement['id']}/reverse",
        json={}, headers=headers,
    )
    assert direct_reverse.status_code == 400
    assert "transaction" in direct_reverse.json()["detail"].lower()
