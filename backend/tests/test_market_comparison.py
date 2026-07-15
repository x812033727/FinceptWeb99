from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from services.market_comparison_service import compare_history, parse_instruments


def _bars(start: date, values: list[float], *, source: str = "test") -> list[dict]:
    return [
        {"date": (start + timedelta(days=i)).isoformat(), "close": value, "data_source": source}
        for i, value in enumerate(values)
    ]


@pytest.mark.asyncio
async def test_compare_history_aligns_common_base_and_metrics():
    cutoff = date.today() - timedelta(days=30)
    us = _bars(cutoff, [100, 105, 110, 120])
    tw = _bars(cutoff + timedelta(days=1), [50, 45, 55])
    with (
        patch("services.market_comparison_service.us_history", new=AsyncMock(return_value=us)),
        patch("services.market_comparison_service.tw_history", new=AsyncMock(return_value=tw)),
    ):
        result = await compare_history("US:AAPL,TW:2330", "1m")

    assert result["common_base_date"] == cutoff + timedelta(days=1)
    assert len(result["series"]) == 2
    apple = next(row for row in result["series"] if row["symbol"] == "AAPL")
    tsmc = next(row for row in result["series"] if row["symbol"] == "2330")
    assert apple["points"][0]["value"] == 100
    assert apple["return_pct"] == pytest.approx((120 / 105 - 1) * 100, abs=1e-4)
    assert apple["max_drawdown_pct"] == 0
    assert tsmc["max_drawdown_pct"] == -10
    assert tsmc["points"][-1]["value"] == 110


@pytest.mark.asyncio
async def test_compare_history_degrades_one_failed_provider():
    cutoff = date.today() - timedelta(days=30)
    with (
        patch("services.market_comparison_service.us_history", new=AsyncMock(side_effect=RuntimeError("down"))),
        patch("services.market_comparison_service.tw_history", new=AsyncMock(return_value=_bars(cutoff, [10, 11]))),
    ):
        result = await compare_history("US:AAPL,TW:2330", "1m")
    assert [row["instrument"] for row in result["series"]] == ["TW:2330"]
    assert result["excluded"] == [{"market": "US", "symbol": "AAPL", "reason": "provider_unavailable"}]


@pytest.mark.parametrize("raw", [
    "US:AAPL", "US:AAPL,US:AAPL", "JP:7203,US:AAPL",
    "US:AAPL;DROP TABLE users,TW:2330", "AAPL,TW:2330",
])
def test_instrument_parser_rejects_invalid_or_unsafe_values(raw: str):
    with pytest.raises(ValueError):
        parse_instruments(raw)


async def _auth(client: AsyncClient) -> dict[str, str]:
    await client.post("/api/auth/register", json={"email": "compare@test.com", "password": "Pass1234!"})
    response = await client.post("/api/auth/login", json={"email": "compare@test.com", "password": "Pass1234!"})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_compare_history_api_auth_validation_and_payload(client: AsyncClient):
    assert (await client.get("/api/global/compare-history?instruments=US:AAPL,TW:2330")).status_code == 401
    headers = await _auth(client)
    cutoff = date.today() - timedelta(days=30)
    with (
        patch("services.market_comparison_service.us_history", new=AsyncMock(return_value=_bars(cutoff, [100, 120]))),
        patch("services.market_comparison_service.tw_history", new=AsyncMock(return_value=_bars(cutoff, [50, 55]))),
    ):
        response = await client.get(
            "/api/global/compare-history?instruments=US:AAPL,TW:2330&period=1m",
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.json()["normalization"] == "first_available_close_equals_100"
    assert {row["instrument"] for row in response.json()["series"]} == {"US:AAPL", "TW:2330"}

    invalid = await client.get(
        "/api/global/compare-history?instruments=US:AAPL&period=1m", headers=headers,
    )
    assert invalid.status_code == 400
