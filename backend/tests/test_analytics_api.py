"""Integration tests for the analytics HTTP API endpoints (DCF, VaR, backtest)."""
import numpy as np
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from models.user import User, UserRole


# ── helpers ───────────────────────────────────────────────────────

async def _register_and_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return r.json()["access_token"]


async def _promote(db: AsyncSession, email: str, role: UserRole = UserRole.analyst) -> None:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one()
    user.role = role
    await db.commit()


def _analyst_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── DCF endpoint ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dcf_requires_analyst(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "dcf_viewer@example.com")
    r = await client.post("/api/analytics/dcf", json={
        "symbol": "2330",
        "market": "TW",
        "overrides": {"fcf_history": [50e9], "shares": 25e9},
    }, headers=_analyst_headers(token))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dcf_requires_auth(client: AsyncClient):
    r = await client.post("/api/analytics/dcf", json={"symbol": "AAPL", "market": "US"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_dcf_with_full_overrides(client: AsyncClient, db_session: AsyncSession):
    """TW market skips fundamentals fetch; full overrides make it self-contained."""
    token = await _register_and_login(client, "dcf_analyst@example.com")
    await _promote(db_session, "dcf_analyst@example.com")

    r = await client.post("/api/analytics/dcf", json={
        "symbol": "2330",
        "market": "TW",
        "overrides": {
            "fcf_history": [200e9, 220e9, 240e9],
            "growth_rate_1": 0.10,
            "growth_rate_2": 0.05,
            "terminal_growth": 0.03,
            "wacc": 0.09,
            "shares": 25.9e9,
            "net_debt": 100e9,
            "current_price": 600.0,
        },
    }, headers=_analyst_headers(token))

    assert r.status_code == 200
    data = r.json()
    assert "intrinsic_value" in data
    assert "equity_value" in data
    assert "pv_fcf" in data
    assert "pv_terminal" in data
    assert "sensitivity" in data
    assert "scenarios" in data
    assert data["intrinsic_value"] > 0
    assert data["symbol"] == "2330"
    assert data["market"] == "TW"


@pytest.mark.asyncio
async def test_dcf_margin_of_safety_present_when_price_given(
    client: AsyncClient, db_session: AsyncSession
):
    token = await _register_and_login(client, "dcf_mos@example.com")
    await _promote(db_session, "dcf_mos@example.com")

    r = await client.post("/api/analytics/dcf", json={
        "symbol": "TEST",
        "market": "TW",
        "overrides": {
            "fcf_history": [10e9],
            "shares": 1e9,
            "current_price": 999.0,
        },
    }, headers=_analyst_headers(token))

    assert r.status_code == 200
    data = r.json()
    assert data["margin_of_safety"] is not None
    # Price of 999 vs low FCF → overvalued → negative MoS
    assert data["margin_of_safety"] < 0


@pytest.mark.asyncio
async def test_dcf_scenarios_shape(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "dcf_scenarios@example.com")
    await _promote(db_session, "dcf_scenarios@example.com")

    r = await client.post("/api/analytics/dcf", json={
        "symbol": "TSMC",
        "market": "TW",
        "overrides": {"fcf_history": [100e9], "shares": 25e9},
    }, headers=_analyst_headers(token))

    assert r.status_code == 200
    scenarios = r.json()["scenarios"]
    assert set(scenarios.keys()) >= {"bull", "base", "bear"}


# ── VaR endpoint ──────────────────────────────────────────────────

def _synthetic_bars(n: int = 252) -> list[dict]:
    """Produce n daily bars with a random walk starting at 100."""
    prices = 100.0 * np.cumprod(1 + np.random.normal(0.0003, 0.015, n))
    return [{"time": f"2023-{(i // 21 + 1):02d}-{(i % 21 + 1):02d}", "close": float(p)}
            for i, p in enumerate(prices)]


@pytest.mark.asyncio
async def test_var_requires_auth(client: AsyncClient):
    r = await client.post("/api/analytics/var", json={
        "symbols": ["AAPL"], "markets": ["US"], "weights": [1.0],
        "portfolio_value": 100000,
    })
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_var_mismatched_lengths(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "var_mismatch@example.com")
    await _promote(db_session, "var_mismatch@example.com")

    r = await client.post("/api/analytics/var", json={
        "symbols": ["AAPL", "MSFT"],
        "markets": ["US"],          # length mismatch
        "weights": [0.5, 0.5],
        "portfolio_value": 100000,
    }, headers=_analyst_headers(token))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_var_historical_method(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "var_hist@example.com")
    await _promote(db_session, "var_hist@example.com")

    bars = _synthetic_bars()
    with patch("services.analytics_service._fetch_returns", new_callable=AsyncMock) as mock_fr:
        import numpy as np
        closes = np.array([b["close"] for b in bars])
        returns = np.diff(closes) / closes[:-1]
        mock_fr.return_value = {"AAPL": returns}

        r = await client.post("/api/analytics/var", json={
            "symbols": ["AAPL"],
            "markets": ["US"],
            "weights": [1.0],
            "portfolio_value": 100000,
            "method": "historical",
            "confidence": 0.95,
            "horizon_days": 1,
        }, headers=_analyst_headers(token))

    assert r.status_code == 200
    data = r.json()
    assert "historical" in data
    assert data["historical"]["var_amount"] > 0
    assert data["historical"]["var_pct"] > 0
    assert "portfolio_metrics" in data


@pytest.mark.asyncio
async def test_var_parametric_method(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "var_param@example.com")
    await _promote(db_session, "var_param@example.com")

    bars = _synthetic_bars()
    with patch("services.analytics_service._fetch_returns", new_callable=AsyncMock) as mock_fr:
        closes = np.array([b["close"] for b in bars])
        returns = np.diff(closes) / closes[:-1]
        mock_fr.return_value = {"AAPL": returns}

        r = await client.post("/api/analytics/var", json={
            "symbols": ["AAPL"],
            "markets": ["US"],
            "weights": [1.0],
            "portfolio_value": 50000,
            "method": "parametric",
            "confidence": 0.99,
        }, headers=_analyst_headers(token))

    assert r.status_code == 200
    assert "parametric" in r.json()


@pytest.mark.asyncio
async def test_var_no_data_returns_400(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "var_nodata@example.com")
    await _promote(db_session, "var_nodata@example.com")

    with patch("services.analytics_service._fetch_returns", new_callable=AsyncMock) as mock_fr:
        mock_fr.return_value = {}   # empty → no price data

        r = await client.post("/api/analytics/var", json={
            "symbols": ["FAKE"],
            "markets": ["US"],
            "weights": [1.0],
            "portfolio_value": 10000,
            "method": "historical",
        }, headers=_analyst_headers(token))

    assert r.status_code == 400


# ── Backtest endpoint ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backtest_requires_auth(client: AsyncClient):
    r = await client.post("/api/analytics/backtest", json={
        "symbols": ["AAPL"], "markets": ["US"],
        "strategy": "sma_crossover", "params": {"fast": 20, "slow": 50},
        "start_date": "2020-01-01", "end_date": "2023-12-31",
    })
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_backtest_invalid_date_range(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "bt_dates@example.com")
    await _promote(db_session, "bt_dates@example.com")

    r = await client.post("/api/analytics/backtest", json={
        "symbols": ["AAPL"], "markets": ["US"],
        "strategy": "sma_crossover", "params": {"fast": 20, "slow": 50},
        "start_date": "2023-12-31", "end_date": "2020-01-01",  # reversed
    }, headers=_analyst_headers(token))
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_backtest_sma_crossover(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "bt_sma@example.com")
    await _promote(db_session, "bt_sma@example.com")

    bars = _synthetic_bars(300)

    with patch("services.us_market_service.get_history", new_callable=AsyncMock) as mock_hist:
        mock_hist.return_value = bars

        r = await client.post("/api/analytics/backtest", json={
            "symbols": ["AAPL"],
            "markets": ["US"],
            "strategy": "sma_crossover",
            "params": {"fast": 10, "slow": 30},
            "start_date": "2023-01-01",
            "end_date": "2024-12-31",
            "initial_capital": 100000,
        }, headers=_analyst_headers(token))

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "completed"
    assert "metrics" in data
    m = data["metrics"]
    assert "total_return_pct" in m
    assert "sharpe_ratio" in m
    assert "max_drawdown_pct" in m
    assert "total_trades" in m
    assert data["metrics"]["final_value"] > 0


@pytest.mark.asyncio
async def test_backtest_rsi_strategy(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "bt_rsi@example.com")
    await _promote(db_session, "bt_rsi@example.com")

    bars = _synthetic_bars(300)

    with patch("services.us_market_service.get_history", new_callable=AsyncMock) as mock_hist:
        mock_hist.return_value = bars

        r = await client.post("/api/analytics/backtest", json={
            "symbols": ["TSLA"],
            "markets": ["US"],
            "strategy": "rsi_mean_reversion",
            "params": {"period": 14, "oversold": 30, "overbought": 70},
            "start_date": "2023-01-01",
            "end_date": "2024-12-31",
            "initial_capital": 50000,
        }, headers=_analyst_headers(token))

    assert r.status_code == 200
    assert r.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_backtest_equity_curve_present(client: AsyncClient, db_session: AsyncSession):
    token = await _register_and_login(client, "bt_equity@example.com")
    await _promote(db_session, "bt_equity@example.com")

    bars = _synthetic_bars(300)

    with patch("services.us_market_service.get_history", new_callable=AsyncMock) as mock_hist:
        mock_hist.return_value = bars

        r = await client.post("/api/analytics/backtest", json={
            "symbols": ["NVDA"],
            "markets": ["US"],
            "strategy": "sma_crossover",
            "params": {"fast": 5, "slow": 20},
            "start_date": "2023-01-01",
            "end_date": "2024-12-31",
            "initial_capital": 100000,
        }, headers=_analyst_headers(token))

    assert r.status_code == 200
    data = r.json()
    assert "equity_curve" in data
    if data["equity_curve"]:
        assert "date" in data["equity_curve"][0]
        assert "value" in data["equity_curve"][0]
