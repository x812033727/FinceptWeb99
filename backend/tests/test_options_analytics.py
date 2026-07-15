from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from services import options_analytics_service as svc

TODAY = date(2026, 7, 15)
EXPIRY = (TODAY + timedelta(days=31)).isoformat()


def contract(kind: str, strike: float, iv: float | None, oi: int | None, volume: int | None = 0, *, expiry: str = EXPIRY):
    return {
        "ticker": f"O:TEST{kind[0]}{strike}",
        "contract_type": kind,
        "expiration_date": expiry,
        "strike_price": strike,
        "last_price": 2.0,
        "bid": 1.9,
        "ask": 2.1,
        "volume": volume,
        "open_interest": oi,
        "implied_volatility": iv,
        "delta": 0.5 if kind == "call" else -0.5,
        "data_source": "yfinance",
    }


def full_chain():
    return [
        contract("call", 90, .24, 10, 20),
        contract("call", 100, .20, 100, 40),
        contract("call", 110, .25, 50, 10),
        contract("put", 90, .30, 50, 30),
        contract("put", 100, .22, 120, 50),
        contract("put", 110, .27, 20, 5),
    ]


def test_computes_atm_term_metrics_skew_expected_move_and_max_pain():
    result = svc.analyze_options_chain(
        "test", full_chain(), spot=100, spot_source="polygon",
        as_of=TODAY,
    )

    assert result["quality"]["status"] == "good"
    assert result["quality"]["iv_coverage_pct"] == 100
    assert result["quality"]["open_interest_coverage_pct"] == 100
    expiry = result["expiries"][0]
    assert expiry["days_to_expiry"] == 31
    assert expiry["atm_iv"] == pytest.approx(.21)
    assert expiry["put_call_open_interest_ratio"] == pytest.approx(190 / 160)
    assert expiry["put_call_volume_ratio"] == pytest.approx(85 / 70)
    assert expiry["wing_skew_iv_points"] == pytest.approx(5.0)
    assert expiry["put_90_strike"] == 90
    assert expiry["call_110_strike"] == 110
    assert expiry["expected_move"] == pytest.approx(100 * .21 * (31 / 365) ** .5)
    assert expiry["max_pain"] == 100
    assert expiry["max_pain_distance_pct"] == 0
    assert "not 25-delta skew" in result["methodology"]["wing_skew"]


def test_sparse_fields_abstain_instead_of_inventing_values():
    rows = [
        contract("call", 100, None, None),
        contract("put", 100, None, None),
        {"contract_type": "call", "strike_price": "bad", "expiration_date": EXPIRY},
    ]
    result = svc.analyze_options_chain("X", rows, spot=None, as_of=TODAY)

    assert result["quality"]["status"] == "degraded"
    assert result["quality"]["rows_received"] == 3
    assert result["quality"]["rows_usable"] == 2
    assert set(result["quality"]["flags"]) >= {
        "spot_unavailable", "iv_sparse", "open_interest_sparse",
    }
    expiry = result["expiries"][0]
    assert expiry["atm_iv"] is None
    assert expiry["expected_move"] is None
    assert expiry["wing_skew_iv_points"] is None
    assert expiry["max_pain"] is None


def test_drops_expired_and_limits_expiry_window_without_marking_data_bad():
    rows = [contract("call", 100, .2, 10, expiry=(TODAY + timedelta(days=i)).isoformat())
            for i in (1, 2, 3)]
    rows.append(contract("put", 100, .2, 10, expiry=(TODAY - timedelta(days=1)).isoformat()))
    result = svc.analyze_options_chain("X", rows, spot=100, as_of=TODAY, max_expiries=2)

    assert [row["days_to_expiry"] for row in result["expiries"]] == [1, 2]
    assert result["quality"]["rows_received"] == 4
    assert result["quality"]["rows_usable"] == 2
    assert "expired_rows_dropped" in result["quality"]["flags"]
    assert "expiry_window_limited" in result["quality"]["flags"]
    assert result["quality"]["status"] == "good"


def test_rejects_implausible_iv_and_nonfinite_numbers():
    rows = [contract("call", 100, 99, 10), contract("put", 100, float("nan"), 5)]
    result = svc.analyze_options_chain("X", rows, spot=100, as_of=TODAY)
    assert result["quality"]["iv_coverage_pct"] == 0
    assert "iv_sparse" in result["quality"]["flags"]
    assert all(row["implied_volatility"] is None for row in result["contracts"])


@pytest.mark.asyncio
async def test_async_analysis_uses_market_services_and_quote_source():
    with patch.object(svc.us_market_service, "get_options", AsyncMock(return_value=full_chain())) as options_mock, \
         patch.object(svc.us_market_service, "get_quote", AsyncMock(return_value={
             "price": 100, "data_source": "polygon",
         })):
        result = await svc.get_options_analysis("test", max_expiries=4)
        options_mock.assert_awaited_once_with("TEST", max_expiries=4)
    assert result["symbol"] == "TEST"
    assert result["spot"] == 100
    assert result["spot_source"] == "polygon"


async def _auth_headers(client: AsyncClient) -> dict[str, str]:
    await client.post("/api/auth/register", json={
        "email": "options-analysis@example.com", "password": "ValidPass99!",
    })
    login = await client.post("/api/auth/login", json={
        "email": "options-analysis@example.com", "password": "ValidPass99!",
    })
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.mark.asyncio
async def test_options_analysis_api_contract_and_bounds(client: AsyncClient):
    headers = await _auth_headers(client)
    payload = svc.analyze_options_chain("AAPL", full_chain(), spot=100, as_of=TODAY)
    with patch.object(svc, "get_options_analysis", AsyncMock(return_value=payload)) as mocked:
        response = await client.get(
            "/api/us/options-analysis/aapl?max_expiries=4", headers=headers,
        )
    assert response.status_code == 200
    assert response.json()["methodology"]["version"] == "options-chain-analytics-v1"
    mocked.assert_awaited_once_with("AAPL", max_expiries=4)
    with patch.object(
        svc, "get_options_analysis",
        AsyncMock(side_effect=RuntimeError("https://provider.test?apiKey=secret")),
    ):
        failed = await client.get("/api/us/options-analysis/AAPL", headers=headers)
    assert failed.status_code == 502
    assert failed.json()["detail"] == "Options analysis unavailable"
    assert "secret" not in failed.text
    assert (await client.get(
        "/api/us/options-analysis/AAPL?max_expiries=13", headers=headers,
    )).status_code == 422
    assert (await client.get("/api/us/options-analysis/AAPL")).status_code == 401
