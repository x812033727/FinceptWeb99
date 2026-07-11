"""Tests for GET /api/portfolio/{id}/risk (feature C1).

Uses in-memory SQLite + mocked Redis (from conftest). Market data
(quotes for weights, 1y history for returns) is mocked at the service
seams — `services.portfolio_service.us_quote` for live values and
`services.portfolio_risk_service.us_history` for the return series —
so tests run without external APIs. Monte Carlo runs through the real
shared ProcessPoolExecutor (same code path production takes).
"""
import numpy as np
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


async def _create_portfolio(client: AsyncClient, token: str, name: str = "RiskFund") -> str:
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


def _synthetic_bars(n: int = 252, seed: int = 0) -> list[dict]:
    """Random-walk price bars suitable for history mocking."""
    rng = np.random.default_rng(seed)
    prices = 100.0 * np.cumprod(1 + rng.normal(0.0003, 0.015, n))
    return [{"time": f"2023-01-{(i % 28) + 1:02d}", "close": float(p), "open": float(p),
             "high": float(p) * 1.01, "low": float(p) * 0.99, "volume": 1_000_000}
            for i, p in enumerate(prices)]


def _mock_quote(price: float = 190.0):
    return patch(
        "services.portfolio_service.us_quote",
        new_callable=AsyncMock,
        return_value={"price": price, "currency": "USD"},
    )


def _mock_history(bars_by_symbol: dict[str, list[dict]] | None = None,
                  default: list[dict] | None = None):
    """Patch the risk service's US history loader. Benchmark (SPY) and
    holdings both flow through it for USD portfolios."""
    async def _hist(symbol: str, period: str = "1y", interval: str = "1d"):
        if bars_by_symbol and symbol in bars_by_symbol:
            return bars_by_symbol[symbol]
        return default if default is not None else []
    return patch(
        "services.portfolio_risk_service.us_history",
        new=AsyncMock(side_effect=_hist),
    )


# ── auth / ownership scoping ──────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_requires_auth(client: AsyncClient):
    r = await client.get("/api/portfolio/00000000-0000-0000-0000-000000000000/risk")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_risk_not_found_returns_404(client: AsyncClient):
    token = await _register_and_login(client, "risk_404@test.com")
    r = await client.get(
        "/api/portfolio/00000000-0000-0000-0000-000000000000/risk",
        headers=_auth(token),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_risk_wrong_user_returns_404(client: AsyncClient):
    tok1 = await _register_and_login(client, "risk_own1@test.com")
    tok2 = await _register_and_login(client, "risk_own2@test.com")
    pid = await _create_portfolio(client, tok1, "Private")
    r = await client.get(f"/api/portfolio/{pid}/risk", headers=_auth(tok2))
    assert r.status_code == 404


# ── empty portfolio ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_empty_portfolio_returns_explicit_empty_state(client: AsyncClient):
    token = await _register_and_login(client, "risk_empty@test.com")
    pid = await _create_portfolio(client, token, "EmptyFund")

    r = await client.get(f"/api/portfolio/{pid}/risk", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert data["empty"] is True
    assert data["var"] == []
    assert data["weights"] == []
    assert data["correlation"] is None
    assert data["metrics"] is None
    assert data["warnings"] == []
    assert data["excluded"] == []
    assert data["portfolio_value"] == 0.0


# ── happy path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_happy_path_two_holdings(client: AsyncClient):
    token = await _register_and_login(client, "risk_happy@test.com")
    pid = await _create_portfolio(client, token)
    await _add_buy(client, token, pid, "AAPL", 180.0, 10)
    await _add_buy(client, token, pid, "MSFT", 400.0, 5)

    bars = {
        "AAPL": _synthetic_bars(252, seed=1),
        "MSFT": _synthetic_bars(252, seed=2),
        "SPY":  _synthetic_bars(252, seed=3),
    }
    with _mock_quote(190.0), _mock_history(bars):
        r = await client.get(f"/api/portfolio/{pid}/risk", headers=_auth(token))

    assert r.status_code == 200
    data = r.json()
    assert data["empty"] is False
    assert data["portfolio_value"] > 0
    assert data["observations"] >= 200
    assert data["benchmark"] == "SPY"

    # Three methods × two confidence levels, all reusing risk.py
    methods = {(v["method"], v["confidence_level"]) for v in data["var"]}
    assert methods == {
        ("historical", 0.95), ("historical", 0.99),
        ("parametric", 0.95), ("parametric", 0.99),
        ("monte_carlo", 0.95), ("monte_carlo", 0.99),
    }
    for v in data["var"]:
        assert v["var_pct"] > 0
        assert v["var_amount"] > 0
    # 99% VaR must be >= 95% VaR within each method
    by_method: dict[str, dict[float, float]] = {}
    for v in data["var"]:
        by_method.setdefault(v["method"], {})[v["confidence_level"]] = v["var_pct"]
    for method, levels in by_method.items():
        assert levels[0.99] >= levels[0.95], method

    # Metrics from analytics.risk.portfolio_metrics — incl. beta vs SPY
    m = data["metrics"]
    for key in ("annualised_return", "annualised_volatility", "sharpe_ratio",
                "sortino_ratio", "calmar_ratio", "max_drawdown"):
        assert key in m
    assert m["beta"] is not None
    assert m["max_drawdown"] <= 0

    # Weights: both holdings, sum ≈ 100, risk contribution present
    assert {w["symbol"] for w in data["weights"]} == {"AAPL", "MSFT"}
    assert sum(w["weight_pct"] for w in data["weights"]) == pytest.approx(100, abs=0.5)
    contribs = [w["risk_contribution_pct"] for w in data["weights"]]
    assert all(c is not None for c in contribs)
    assert sum(contribs) == pytest.approx(100, abs=1.0)

    # Correlation matrix: 2×2, unit diagonal, symmetric
    corr = data["correlation"]
    assert corr["symbols"] == ["AAPL", "MSFT"]
    assert corr["matrix"][0][0] == pytest.approx(1.0, abs=1e-6)
    assert corr["matrix"][1][1] == pytest.approx(1.0, abs=1e-6)
    assert corr["matrix"][0][1] == pytest.approx(corr["matrix"][1][0], abs=1e-6)

    # 2 US holdings → market bucket US = 100% > 50% warning; and one
    # of the positions necessarily exceeds 25%.
    kinds = {w["kind"] for w in data["warnings"]}
    assert "market_bucket" in kinds
    assert "single_position" in kinds
    bucket = next(w for w in data["warnings"] if w["kind"] == "market_bucket")
    assert bucket["key"] == "US"
    assert bucket["weight_pct"] == pytest.approx(100, abs=0.5)

    assert data["excluded"] == []


# ── graceful degradation ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_risk_insufficient_history_lands_in_excluded(client: AsyncClient):
    token = await _register_and_login(client, "risk_excl@test.com")
    pid = await _create_portfolio(client, token)
    await _add_buy(client, token, pid, "AAPL", 180.0, 10)
    await _add_buy(client, token, pid, "NEWIPO", 50.0, 100)

    bars = {
        "AAPL": _synthetic_bars(252, seed=1),
        "NEWIPO": _synthetic_bars(5, seed=2),   # < MIN_OBSERVATIONS
        "SPY":  _synthetic_bars(252, seed=3),
    }
    with _mock_quote(190.0), _mock_history(bars):
        r = await client.get(f"/api/portfolio/{pid}/risk", headers=_auth(token))

    assert r.status_code == 200
    data = r.json()
    assert data["empty"] is False
    # Excluded holding is reported, not fatal
    assert len(data["excluded"]) == 1
    exc = data["excluded"][0]
    assert exc["symbol"] == "NEWIPO"
    assert exc["reason"] == "insufficient_history"
    # Correlation matrix only covers holdings with usable history
    assert data["correlation"]["symbols"] == ["AAPL"]
    # …but weights still list ALL holdings (excluded ones count toward
    # concentration), with no risk contribution for the excluded one
    assert {w["symbol"] for w in data["weights"]} == {"AAPL", "NEWIPO"}
    newipo = next(w for w in data["weights"] if w["symbol"] == "NEWIPO")
    assert newipo["risk_contribution_pct"] is None
    # VaR still computed from the remaining holding
    assert len(data["var"]) == 6


@pytest.mark.asyncio
async def test_risk_all_holdings_excluded_returns_degraded_200(client: AsyncClient):
    token = await _register_and_login(client, "risk_allexcl@test.com")
    pid = await _create_portfolio(client, token)
    await _add_buy(client, token, pid, "AAPL", 180.0, 10)

    with _mock_quote(190.0), _mock_history(default=[]):
        r = await client.get(f"/api/portfolio/{pid}/risk", headers=_auth(token))

    assert r.status_code == 200
    data = r.json()
    assert data["empty"] is False        # portfolio HAS holdings
    assert data["var"] == []
    assert data["metrics"] is None
    assert data["correlation"] is None
    assert len(data["excluded"]) == 1
    assert data["excluded"][0]["symbol"] == "AAPL"
    # Concentration warnings still fire off weights alone
    assert any(w["kind"] == "single_position" for w in data["warnings"])
