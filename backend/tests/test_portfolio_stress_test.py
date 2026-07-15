from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from services.portfolio_stress_service import stress_test_portfolio

_DETAIL = {
    "id": "p1", "name": "Mixed", "currency": "TWD", "total_value": 100_000.0,
    "total_cost": 90_000.0, "total_pnl": 10_000.0, "total_pnl_pct": 11.11,
    "holdings": [
        {"symbol": "2330", "market": "TW", "current_value": 60_000.0},
        {"symbol": "AAPL", "market": "US", "current_value": 40_000.0},
    ],
}


@pytest.mark.asyncio
async def test_stress_test_outputs_pnl_contribution_and_rebalance():
    with patch(
        "services.portfolio_stress_service.get_portfolio_detail",
        new=AsyncMock(return_value=_DETAIL),
    ):
        result = await stress_test_portfolio(
            "p1", "u1", None, scenarios=["taiex_drawdown", "twd_depreciation", "single_stock_gap"],
        )

    taiex, fx, gap = result["scenarios"]
    assert taiex["pnl"] == -7200.0
    assert taiex["pnl_pct"] == -7.2
    assert round(sum(row["risk_contribution_pct"] for row in taiex["holdings"]), 1) == 100.0
    assert taiex["rebalance_suggestions"][0]["symbol"] == "2330"
    assert fx["pnl"] == 2000.0  # USD holding translation into TWD
    assert result["gap_symbol"] == "2330"  # largest holding by default
    assert gap["pnl"] == -12000.0
    assert "not a forecast" in result["disclaimer"]


@pytest.mark.asyncio
async def test_stress_test_rejects_unknown_scenario():
    with patch(
        "services.portfolio_stress_service.get_portfolio_detail",
        new=AsyncMock(return_value=_DETAIL),
    ):
        with pytest.raises(ValueError, match="Unknown scenarios"):
            await stress_test_portfolio("p1", "u1", None, scenarios=["magic_rally"])


async def _token(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    response = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_stress_api_owner_scope_returns_404(client: AsyncClient):
    owner = await _token(client, "stress-owner@test.com")
    stranger = await _token(client, "stress-stranger@test.com")
    created = await client.post(
        "/api/portfolio", headers={"Authorization": f"Bearer {owner}"},
        json={"name": "Private", "currency": "TWD"},
    )
    portfolio_id = created.json()["id"]
    response = await client.post(
        f"/api/portfolio/{portfolio_id}/stress-test",
        headers={"Authorization": f"Bearer {stranger}"}, json={},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stress_api_returns_all_named_scenarios_for_empty_portfolio(client: AsyncClient):
    token = await _token(client, "stress-empty@test.com")
    created = await client.post(
        "/api/portfolio", headers={"Authorization": f"Bearer {token}"},
        json={"name": "Empty", "currency": "USD"},
    )
    response = await client.post(
        f"/api/portfolio/{created.json()['id']}/stress-test",
        headers={"Authorization": f"Bearer {token}"}, json={},
    )
    assert response.status_code == 200
    assert {row["scenario"] for row in response.json()["scenarios"]} == {
        "taiex_drawdown", "semiconductor_downturn", "twd_depreciation",
        "rates_up_100bp", "single_stock_gap",
    }
