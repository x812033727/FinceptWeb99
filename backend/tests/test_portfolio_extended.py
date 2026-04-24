"""
Extended portfolio integration tests — portfolio detail, performance snapshots,
and the mean-variance optimiser endpoint.
"""
import numpy as np
import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


# ── shared helpers ────────────────────────────────────────────────

async def _register_and_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Test1234!"})
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_portfolio(client: AsyncClient, token: str, name: str = "Fund") -> str:
    r = await client.post(
        "/api/portfolio",
        json={"name": name, "currency": "USD"},
        headers=_auth(token),
    )
    return r.json()["id"]


async def _add_buy(
    client: AsyncClient,
    token: str,
    portfolio_id: str,
    symbol: str = "AAPL",
    price: float = 180.0,
    qty: int = 10,
) -> None:
    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": price, "currency": "USD"}
        await client.post(
            f"/api/portfolio/{portfolio_id}/transaction",
            json={
                "symbol": symbol, "market": "US", "tx_type": "buy",
                "quantity": qty, "price": price, "fx_rate": 1.0,
                "tx_date": "2024-01-15",
            },
            headers=_auth(token),
        )


def _synthetic_bars(n: int = 252) -> list[dict]:
    """Random-walk price bars suitable for history mocking."""
    prices = 100.0 * np.cumprod(1 + np.random.normal(0.0003, 0.015, n))
    return [{"time": f"2023-01-{(i % 28) + 1:02d}", "close": float(p), "open": float(p),
             "high": float(p) * 1.01, "low": float(p) * 0.99, "volume": 1_000_000}
            for i, p in enumerate(prices)]


# ── portfolio detail ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_detail_no_holdings(client: AsyncClient):
    token = await _register_and_login(client, "pd_empty@test.com")
    pid = await _create_portfolio(client, token, "EmptyFund")

    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": 0, "currency": "USD"}
        r = await client.get(f"/api/portfolio/{pid}", headers=_auth(token))

    assert r.status_code == 200
    data = r.json()
    assert data["holdings"] == []
    assert data["currency"] == "USD"
    assert "total_value" in data


@pytest.mark.asyncio
async def test_portfolio_detail_with_holding(client: AsyncClient):
    token = await _register_and_login(client, "pd_hold@test.com")
    pid = await _create_portfolio(client, token, "HoldFund")
    await _add_buy(client, token, pid, "AAPL", 180.0, 10)

    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": 190.0, "currency": "USD"}
        r = await client.get(f"/api/portfolio/{pid}", headers=_auth(token))

    assert r.status_code == 200
    data = r.json()
    holdings = data["holdings"]
    assert len(holdings) == 1
    h = holdings[0]
    assert h["symbol"] == "AAPL"
    assert h["market"] == "US"
    assert abs(h["quantity"] - 10.0) < 0.01
    assert abs(h["avg_cost"] - 180.0) < 0.01
    assert "current_price" in h
    assert "unrealized_pnl" in h
    assert "unrealized_pnl_pct" in h


@pytest.mark.asyncio
async def test_portfolio_detail_requires_auth(client: AsyncClient):
    await _register_and_login(client, "pd_auth@test.com")
    import uuid
    r = await client.get(f"/api/portfolio/{uuid.uuid4()}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_portfolio_detail_not_found(client: AsyncClient):
    token = await _register_and_login(client, "pd_404@test.com")
    import uuid
    r = await client.get(f"/api/portfolio/{uuid.uuid4()}", headers=_auth(token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_portfolio_detail_wrong_user(client: AsyncClient):
    tok1 = await _register_and_login(client, "pd_u1@test.com")
    tok2 = await _register_and_login(client, "pd_u2@test.com")
    pid = await _create_portfolio(client, tok1, "PrivateFund")

    r = await client.get(f"/api/portfolio/{pid}", headers=_auth(tok2))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_portfolio_detail_pnl_sign(client: AsyncClient):
    """Unrealised P&L should be positive when current price > avg cost."""
    token = await _register_and_login(client, "pd_pnl@test.com")
    pid = await _create_portfolio(client, token, "PnLFund")
    await _add_buy(client, token, pid, "MSFT", 400.0, 5)

    with patch("services.portfolio_service.us_quote", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = {"price": 450.0, "currency": "USD"}   # +50 per share
        r = await client.get(f"/api/portfolio/{pid}", headers=_auth(token))

    assert r.status_code == 200
    h = r.json()["holdings"][0]
    assert (h["unrealized_pnl"] or 0) > 0
    assert (h["unrealized_pnl_pct"] or 0) > 0


# ── portfolio performance ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_portfolio_performance_empty(client: AsyncClient):
    """Fresh portfolio has no daily snapshots → empty list."""
    token = await _register_and_login(client, "perf_empty@test.com")
    pid = await _create_portfolio(client, token, "PerfFund")

    r = await client.get(f"/api/portfolio/{pid}/performance?days=90", headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_portfolio_performance_requires_auth(client: AsyncClient):
    await _register_and_login(client, "perf_auth@test.com")
    import uuid
    r = await client.get(f"/api/portfolio/{uuid.uuid4()}/performance")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_portfolio_performance_wrong_user_returns_empty(client: AsyncClient):
    tok1 = await _register_and_login(client, "perf_u1@test.com")
    tok2 = await _register_and_login(client, "perf_u2@test.com")
    pid = await _create_portfolio(client, tok1, "PerfPrivate")

    r = await client.get(f"/api/portfolio/{pid}/performance", headers=_auth(tok2))
    assert r.status_code == 200
    assert r.json() == []


# ── portfolio optimiser ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_optimise_requires_auth(client: AsyncClient):
    await _register_and_login(client, "opt_auth@test.com")
    import uuid
    r = await client.post(
        f"/api/portfolio/{uuid.uuid4()}/optimise",
        json={"target_risk": "moderate", "max_weight": 1.0},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_optimise_no_holdings_returns_400(client: AsyncClient):
    token = await _register_and_login(client, "opt_empty@test.com")
    pid = await _create_portfolio(client, token, "EmptyOpt")

    r = await client.post(
        f"/api/portfolio/{pid}/optimise",
        json={"target_risk": "moderate", "max_weight": 1.0},
        headers=_auth(token),
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_optimise_not_found_returns_400(client: AsyncClient):
    token = await _register_and_login(client, "opt_404@test.com")
    import uuid
    r = await client.post(
        f"/api/portfolio/{uuid.uuid4()}/optimise",
        json={"target_risk": "moderate", "max_weight": 1.0},
        headers=_auth(token),
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_optimise_with_holdings(client: AsyncClient):
    """Full optimiser run with mocked price history."""
    token = await _register_and_login(client, "opt_run@test.com")
    pid = await _create_portfolio(client, token, "OptFund")

    # Add two holdings
    await _add_buy(client, token, pid, "AAPL", 180.0, 10)
    await _add_buy(client, token, pid, "MSFT", 400.0, 5)

    bars = _synthetic_bars(252)

    with patch("services.portfolio_service.us_history", new_callable=AsyncMock) as mock_hist:
        mock_hist.return_value = bars

        r = await client.post(
            f"/api/portfolio/{pid}/optimise",
            json={"target_risk": "moderate", "max_weight": 0.8},
            headers=_auth(token),
        )

    assert r.status_code == 200
    data = r.json()
    assert "weights" in data
    assert "metrics" in data
    assert set(data["weights"].keys()) == {"AAPL", "MSFT"}
    # Weights should sum to ~1
    total = sum(data["weights"].values())
    assert abs(total - 1.0) < 0.02
    # Metrics should have key fields
    metrics = data["metrics"]
    assert "expected_annual_return" in metrics
    assert "annual_volatility" in metrics
    assert "sharpe_ratio" in metrics


@pytest.mark.asyncio
async def test_optimise_target_risk_variants(client: AsyncClient):
    """Confirm all target_risk values are accepted."""
    token = await _register_and_login(client, "opt_risk@test.com")
    pid = await _create_portfolio(client, token, "RiskFund")
    await _add_buy(client, token, pid, "AAPL", 180.0, 10)
    await _add_buy(client, token, pid, "TSLA", 250.0, 4)

    bars = _synthetic_bars(252)

    for risk in ("conservative", "moderate", "aggressive"):
        with patch("services.portfolio_service.us_history", new_callable=AsyncMock) as mock_hist:
            mock_hist.return_value = bars
            r = await client.post(
                f"/api/portfolio/{pid}/optimise",
                json={"target_risk": risk, "max_weight": 1.0},
                headers=_auth(token),
            )
        assert r.status_code == 200, f"risk={risk} returned {r.status_code}: {r.text}"
