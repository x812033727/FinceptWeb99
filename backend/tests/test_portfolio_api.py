"""
Integration tests for portfolio endpoints.
Uses in-memory SQLite + mocked Redis (from conftest).
Market data calls are mocked so tests run without external APIs.
"""
import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


# ── helpers ───────────────────────────────────────────────────────

async def _register_and_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Test1234!"})
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── portfolio CRUD ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_portfolio(client: AsyncClient):
    token = await _register_and_login(client, "pf_create@test.com")
    r = await client.post("/api/portfolio", json={"name": "My Fund", "currency": "USD"}, headers=_auth(token))
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "My Fund"
    assert body["currency"] == "USD"
    assert "id" in body


@pytest.mark.asyncio
async def test_list_portfolios_empty(client: AsyncClient):
    token = await _register_and_login(client, "pf_empty@test.com")
    r = await client.get("/api/portfolio", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_portfolios_after_create(client: AsyncClient):
    token = await _register_and_login(client, "pf_list@test.com")
    await client.post("/api/portfolio", json={"name": "Alpha", "currency": "USD"}, headers=_auth(token))
    await client.post("/api/portfolio", json={"name": "Beta",  "currency": "TWD"}, headers=_auth(token))
    r = await client.get("/api/portfolio", headers=_auth(token))
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert {"Alpha", "Beta"} == names


@pytest.mark.asyncio
async def test_delete_portfolio(client: AsyncClient):
    token = await _register_and_login(client, "pf_delete@test.com")
    cr = await client.post("/api/portfolio", json={"name": "ToDelete", "currency": "USD"}, headers=_auth(token))
    pid = cr.json()["id"]
    dr = await client.delete(f"/api/portfolio/{pid}", headers=_auth(token))
    assert dr.status_code == 204
    # Verify gone
    lr = await client.get("/api/portfolio", headers=_auth(token))
    assert all(p["id"] != pid for p in lr.json())


@pytest.mark.asyncio
async def test_delete_nonexistent_portfolio_returns_404(client: AsyncClient):
    token = await _register_and_login(client, "pf_del404@test.com")
    r = await client.delete("/api/portfolio/00000000-0000-0000-0000-000000000000", headers=_auth(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_portfolio_not_accessible_by_other_user(client: AsyncClient):
    tok1 = await _register_and_login(client, "pf_own1@test.com")
    tok2 = await _register_and_login(client, "pf_own2@test.com")
    cr = await client.post("/api/portfolio", json={"name": "Private", "currency": "USD"}, headers=_auth(tok1))
    pid = cr.json()["id"]
    r = await client.delete(f"/api/portfolio/{pid}", headers=_auth(tok2))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_portfolio_requires_auth(client: AsyncClient):
    r = await client.get("/api/portfolio")
    assert r.status_code == 401


# ── transactions ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_transaction(client: AsyncClient):
    token = await _register_and_login(client, "pf_tx@test.com")
    cr = await client.post("/api/portfolio", json={"name": "TxTest", "currency": "USD"}, headers=_auth(token))
    pid = cr.json()["id"]

    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": 180.0, "currency": "USD"}
        r = await client.post(
            f"/api/portfolio/{pid}/transaction",
            json={
                "symbol": "AAPL", "market": "US", "tx_type": "buy",
                "quantity": 10, "price": 180.0, "fx_rate": 1.0,
                "tx_date": "2024-01-15",
            },
            headers=_auth(token),
        )
    assert r.status_code == 201
    assert r.json()["status"] == "created"


@pytest.mark.asyncio
async def test_transaction_import_previews_then_commits_clean_batch(client: AsyncClient):
    token = await _register_and_login(client, "pf_import@test.com")
    cr = await client.post(
        "/api/portfolio", json={"name": "Imported", "currency": "USD"},
        headers=_auth(token),
    )
    pid = cr.json()["id"]
    # Use the same broker-neutral columns emitted by the frontend exporter.
    rows = [
        {"tx_date": "2024-01-02", "symbol": "aapl", "market": "us", "tx_type": "buy",
         "quantity": 10, "price": 100, "fx_rate": 1, "notes": "opening"},
        {"tx_date": "2024-01-03", "symbol": "AAPL", "market": "US", "tx_type": "sell",
         "quantity": 4, "price": 110, "fx_rate": 1},
    ]

    preview = await client.post(
        f"/api/portfolio/{pid}/transactions/import",
        json={"rows": rows, "dry_run": True}, headers=_auth(token),
    )
    assert preview.status_code == 200
    assert preview.json() == {
        "valid": True, "valid_count": 2, "imported_count": 0, "errors": [],
    }
    assert (await client.get(
        f"/api/portfolio/{pid}/transactions", headers=_auth(token),
    )).json() == []

    committed = await client.post(
        f"/api/portfolio/{pid}/transactions/import",
        json={"rows": rows, "dry_run": False}, headers=_auth(token),
    )
    assert committed.status_code == 200
    assert committed.json()["imported_count"] == 2
    transactions = (await client.get(
        f"/api/portfolio/{pid}/transactions", headers=_auth(token),
    )).json()
    assert len(transactions) == 2
    detail = (await client.get(f"/api/portfolio/{pid}", headers=_auth(token))).json()
    assert detail["holdings"][0]["quantity"] == 6


@pytest.mark.asyncio
async def test_transaction_import_reports_rows_and_writes_nothing(client: AsyncClient):
    token = await _register_and_login(client, "pf_import_invalid@test.com")
    cr = await client.post(
        "/api/portfolio", json={"name": "Invalid import", "currency": "USD"},
        headers=_auth(token),
    )
    pid = cr.json()["id"]
    response = await client.post(
        f"/api/portfolio/{pid}/transactions/import",
        json={
            "dry_run": False,
            "rows": [
                {"tx_date": "2024-01-02", "symbol": "AAPL", "market": "US",
                 "tx_type": "sell", "quantity": 1, "price": 100},
                {"tx_date": "not-a-date", "symbol": "BAD!", "market": "EU",
                 "tx_type": "buy", "quantity": 0, "price": -1},
            ],
        },
        headers=_auth(token),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is False
    assert {error["row"] for error in result["errors"]} == {2, 3}
    assert any(
        error["row"] == 2 and error["field"] == "quantity"
        for error in result["errors"]
    )
    assert (await client.get(
        f"/api/portfolio/{pid}/transactions", headers=_auth(token),
    )).json() == []


@pytest.mark.asyncio
async def test_list_transactions_empty(client: AsyncClient):
    token = await _register_and_login(client, "pf_txlist_empty@test.com")
    cr = await client.post("/api/portfolio", json={"name": "EmptyTx", "currency": "USD"}, headers=_auth(token))
    pid = cr.json()["id"]
    r = await client.get(f"/api/portfolio/{pid}/transactions", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_transactions_after_add(client: AsyncClient):
    token = await _register_and_login(client, "pf_txlist@test.com")
    cr = await client.post("/api/portfolio", json={"name": "TxListTest", "currency": "USD"}, headers=_auth(token))
    pid = cr.json()["id"]

    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": 180.0, "currency": "USD"}
        for i in range(3):
            await client.post(
                f"/api/portfolio/{pid}/transaction",
                json={
                    "symbol": "AAPL", "market": "US", "tx_type": "buy",
                    "quantity": 5, "price": 180.0 + i, "fx_rate": 1.0,
                    "tx_date": f"2024-01-{15 + i:02d}",
                },
                headers=_auth(token),
            )

    r = await client.get(f"/api/portfolio/{pid}/transactions", headers=_auth(token))
    assert r.status_code == 200
    txns = r.json()
    assert len(txns) == 3
    # Newest first
    assert txns[0]["tx_date"] >= txns[-1]["tx_date"]


@pytest.mark.asyncio
async def test_transaction_fields_present(client: AsyncClient):
    token = await _register_and_login(client, "pf_txfields@test.com")
    cr = await client.post("/api/portfolio", json={"name": "FieldTest", "currency": "USD"}, headers=_auth(token))
    pid = cr.json()["id"]

    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": 200.0, "currency": "USD"}
        await client.post(
            f"/api/portfolio/{pid}/transaction",
            json={
                "symbol": "MSFT", "market": "US", "tx_type": "buy",
                "quantity": 2, "price": 400.0, "fx_rate": 1.0,
                "tx_date": "2024-03-01", "notes": "Initial purchase",
            },
            headers=_auth(token),
        )

    r = await client.get(f"/api/portfolio/{pid}/transactions", headers=_auth(token))
    tx = r.json()[0]
    assert tx["symbol"] == "MSFT"
    assert tx["market"] == "US"
    assert tx["tx_type"] == "buy"
    assert tx["quantity"] == 2.0
    assert tx["price"] == pytest.approx(400.0, rel=1e-3)
    assert tx["notes"] == "Initial purchase"
    assert tx["tx_date"] == "2024-03-01"
    assert "id" in tx
    assert "created_at" in tx


@pytest.mark.asyncio
async def test_transactions_not_accessible_by_other_user(client: AsyncClient):
    tok1 = await _register_and_login(client, "pf_txown1@test.com")
    tok2 = await _register_and_login(client, "pf_txown2@test.com")
    cr = await client.post("/api/portfolio", json={"name": "PrivateTx", "currency": "USD"}, headers=_auth(tok1))
    pid = cr.json()["id"]
    r = await client.get(f"/api/portfolio/{pid}/transactions", headers=_auth(tok2))
    # Other-owned portfolios are 404 (preserves the same not-leaking-existence
    # semantic we use elsewhere — matches GET /api/portfolio/{id}).
    assert r.status_code == 404
