"""
Unit tests for tw_market_service.get_health — pure pivot + ratio + light
logic. Mocks FinMind connectors and the Redis cache layer so the test
runs offline without aiosqlite/passlib (matches the local-runnable
pure-unit-test set called out in CLAUDE.md).
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as svc


def _income_rows(date: str, revenue: float, gross_profit: float,
                 operating_income: float, net_income: float, eps: float = 0.0) -> list[dict]:
    return [
        {"date": date, "type": "Revenue",         "value": revenue},
        {"date": date, "type": "GrossProfit",     "value": gross_profit},
        {"date": date, "type": "OperatingIncome", "value": operating_income},
        {"date": date, "type": "NetIncome",       "value": net_income},
        {"date": date, "type": "EPS",             "value": eps},
    ]


def _balance_rows(date: str, assets: float, liabilities: float, equity: float,
                  current_assets: float = 0.0, current_liabilities: float = 0.0) -> list[dict]:
    return [
        {"date": date, "type": "TotalAssets",        "value": assets},
        {"date": date, "type": "TotalLiabilities",   "value": liabilities},
        {"date": date, "type": "Equity",             "value": equity},
        {"date": date, "type": "CurrentAssets",      "value": current_assets},
        {"date": date, "type": "CurrentLiabilities", "value": current_liabilities},
    ]


def _cash_flow_rows(date: str, operating_cf: float,
                    capex: float = -10.0) -> list[dict]:
    return [
        {"date": date, "type": "CashFlowsFromOperatingActivities", "value": operating_cf},
        {"date": date, "type": "AcquisitionOfPropertyPlantAndEquipment", "value": capex},
    ]


@pytest.mark.asyncio
async def test_get_health_pivots_and_scores_lights():
    """Verify pivot, derived ratios, TTM ROE, and traffic-light thresholds."""
    income = (
        _income_rows("2023-03-31", 100, 50, 40, 30, 1.0) +
        _income_rows("2023-06-30", 110, 56, 45, 33, 1.1) +
        _income_rows("2023-09-30", 120, 62, 50, 36, 1.2) +
        _income_rows("2023-12-31", 130, 68, 55, 40, 1.3)
    )
    bs = (
        _balance_rows("2023-03-31", 1000, 300, 700, 500, 200) +
        _balance_rows("2023-06-30", 1050, 320, 730, 520, 210) +
        _balance_rows("2023-09-30", 1100, 340, 760, 540, 220) +
        _balance_rows("2023-12-31", 1150, 360, 800, 560, 230)
    )
    cf = (
        _cash_flow_rows("2023-03-31", 25) +
        _cash_flow_rows("2023-06-30", 28) +
        _cash_flow_rows("2023-09-30", 31) +
        _cash_flow_rows("2023-12-31", 34)
    )

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_financials", new_callable=AsyncMock, return_value=income), \
         patch.object(svc.finmind, "get_balance_sheet", new_callable=AsyncMock, return_value=bs), \
         patch.object(svc.finmind, "get_cash_flow", new_callable=AsyncMock, return_value=cf), \
         patch.object(svc, "get_revenue", new_callable=AsyncMock, return_value=[
             {"date": "2024-01", "revenue_yoy": 18.0},
         ]):
        result = await svc.get_health("2330", periods=4)

    assert result["symbol"] == "2330"
    assert len(result["periods"]) == 4

    latest = result["periods"][-1]
    assert latest["date"] == "2023-12-31"
    # gross_margin = 68/130 = 52.31%
    assert latest["gross_margin"] == pytest.approx(52.31, abs=0.01)
    assert latest["operating_margin"] == pytest.approx(42.31, abs=0.01)
    assert latest["net_margin"] == pytest.approx(30.77, abs=0.01)
    # debt_ratio = 360/1150 = 31.30%
    assert latest["debt_ratio"] == pytest.approx(31.30, abs=0.01)
    # current_ratio = 560/230
    assert latest["current_ratio"] == pytest.approx(2.43, abs=0.01)

    summary = result["summary"]
    # TTM net income = 30+33+36+40 = 139; equity = 800; ROE = 17.375%
    assert summary["latest_roe"] == pytest.approx(17.38, abs=0.01)
    assert summary["revenue_yoy"] == pytest.approx(18.0)
    assert summary["cf_positive_streak_4q"] == 4

    lights = result["lights"]
    # ROE 17% > 15 → green
    assert lights["profitability"] == "green"
    # debt_ratio 31% < 50 → green
    assert lights["safety"] == "green"
    # revenue_yoy 18% > 10 → green
    assert lights["growth"] == "green"
    # 4/4 quarters positive → green
    assert lights["cash_flow"] == "green"


@pytest.mark.asyncio
async def test_get_health_red_lights_when_metrics_weak():
    """High debt, negative growth, negative cash flow → red lights."""
    income = (
        _income_rows("2023-03-31", 100, 10, 1, -5, -0.1) +
        _income_rows("2023-06-30", 100, 10, 1, -5, -0.1) +
        _income_rows("2023-09-30", 100, 10, 1, -5, -0.1) +
        _income_rows("2023-12-31", 100, 10, 1, -5, -0.1)
    )
    # debt 80%, equity 200
    bs = (
        _balance_rows("2023-03-31", 1000, 800, 200, 100, 200) +
        _balance_rows("2023-06-30", 1000, 800, 200, 100, 200) +
        _balance_rows("2023-09-30", 1000, 800, 200, 100, 200) +
        _balance_rows("2023-12-31", 1000, 800, 200, 100, 200)
    )
    cf = (
        _cash_flow_rows("2023-03-31", -10) +
        _cash_flow_rows("2023-06-30", -8) +
        _cash_flow_rows("2023-09-30", -12) +
        _cash_flow_rows("2023-12-31", -15)
    )

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_financials", new_callable=AsyncMock, return_value=income), \
         patch.object(svc.finmind, "get_balance_sheet", new_callable=AsyncMock, return_value=bs), \
         patch.object(svc.finmind, "get_cash_flow", new_callable=AsyncMock, return_value=cf), \
         patch.object(svc, "get_revenue", new_callable=AsyncMock, return_value=[
             {"date": "2024-01", "revenue_yoy": -8.0},
         ]):
        result = await svc.get_health("9999", periods=4)

    lights = result["lights"]
    # ROE = (-20) / 200 = -10% → red
    assert lights["profitability"] == "red"
    # debt 80% → red (>70)
    assert lights["safety"] == "red"
    # revenue_yoy -8 → red
    assert lights["growth"] == "red"
    # 0/4 positive → red
    assert lights["cash_flow"] == "red"


@pytest.mark.asyncio
async def test_get_health_returns_gray_when_data_missing():
    """All connectors empty → no periods, gray lights, no crash."""
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_financials", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc.finmind, "get_balance_sheet", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc.finmind, "get_cash_flow", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc, "get_revenue", new_callable=AsyncMock, return_value=[]):
        result = await svc.get_health("0000")

    assert result["periods"] == []
    assert result["lights"]["profitability"] == "gray"
    assert result["lights"]["safety"] == "gray"
    assert result["lights"]["growth"] == "gray"
    assert result["lights"]["cash_flow"] == "gray"


def test_pick_returns_first_non_none_alias():
    period = {"OperatingRevenue": 250.0, "Revenue": None}
    assert svc._pick(period, ("Revenue", "OperatingRevenue")) == 250.0


def test_pick_returns_none_when_no_match():
    period = {"Foo": 1.0}
    assert svc._pick(period, ("Bar", "Baz")) is None


def test_safe_div_handles_zero_and_none():
    assert svc._safe_div(10, 2) == 5
    assert svc._safe_div(None, 2) is None
    assert svc._safe_div(10, 0) is None
    assert svc._safe_div(10, None) is None


def test_light_higher_better_thresholds():
    assert svc._light(20, green=15, yellow=5) == "green"
    assert svc._light(10, green=15, yellow=5) == "yellow"
    assert svc._light(0, green=15, yellow=5) == "red"
    assert svc._light(None, green=15, yellow=5) == "gray"


def test_light_lower_better_thresholds():
    # debt-ratio style: lower is better
    assert svc._light(40, green=50, yellow=70, higher_better=False) == "green"
    assert svc._light(60, green=50, yellow=70, higher_better=False) == "yellow"
    assert svc._light(80, green=50, yellow=70, higher_better=False) == "red"


# ── valuation-band helpers ──────────────────────────────────────


def test_ttm_eps_at_sums_last_four_quarters():
    history = [
        ("2022-12-31", 1.0), ("2023-03-31", 1.2),
        ("2023-06-30", 1.4), ("2023-09-30", 1.6), ("2023-12-31", 2.0),
    ]
    # As of 2023-12-31: last 4 = 1.2 + 1.4 + 1.6 + 2.0 = 6.2
    assert svc._ttm_eps_at("2023-12-31", history) == pytest.approx(6.2)
    # As of 2023-04-01: last 4 ≤ 2023-04-01 = [1.0, 1.2] = 2.2
    assert svc._ttm_eps_at("2023-04-01", history) == pytest.approx(2.2)
    # Before any reports
    assert svc._ttm_eps_at("2020-01-01", history) is None


def test_bvps_at_uses_latest_available_data():
    equity = [("2023-06-30", 1000.0), ("2023-12-31", 1200.0)]
    shares = [("2023-06-30", 100.0),  ("2023-12-31", 100.0)]
    assert svc._bvps_at("2024-01-01", equity, shares) == pytest.approx(12.0)
    assert svc._bvps_at("2023-09-15", equity, shares) == pytest.approx(10.0)
    # Zero shares → None
    assert svc._bvps_at("2024-01-01", equity, [("2023-12-31", 0)]) is None
    # Before any data → None
    assert svc._bvps_at("2020-01-01", equity, shares) is None


def test_stats_returns_mean_std_percentiles():
    s = svc._stats([10, 20, 30, 40, 50])
    assert s["mean"] == pytest.approx(30.0)
    assert s["min"] == 10
    assert s["max"] == 50
    assert s["p50"] == pytest.approx(30.0)
    # std of 10..50 is sqrt(200) ≈ 14.142
    assert s["std"] == pytest.approx(14.142, abs=0.01)


def test_stats_handles_empty_input():
    s = svc._stats([])
    assert s["mean"] is None
    assert s["std"] is None
    assert s["p50"] is None


@pytest.mark.asyncio
async def test_get_valuation_band_pe_basic():
    """Series, stats, and current_z populated when EPS + prices align."""
    bars = [
        {"time": "2023-06-30", "close": 100.0},
        {"time": "2023-09-30", "close": 120.0},
        {"time": "2023-12-31", "close": 150.0},
    ]
    income = [
        {"date": "2022-12-31", "type": "EPS", "value": 2.0},
        {"date": "2023-03-31", "type": "EPS", "value": 2.0},
        {"date": "2023-06-30", "type": "EPS", "value": 2.0},
        {"date": "2023-09-30", "type": "EPS", "value": 2.0},
        {"date": "2023-12-31", "type": "EPS", "value": 2.0},
    ]
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "get_history", new_callable=AsyncMock, return_value=bars), \
         patch.object(svc.finmind, "get_financials", new_callable=AsyncMock, return_value=income), \
         patch.object(svc.finmind, "get_balance_sheet", new_callable=AsyncMock, return_value=[]):
        result = await svc.get_valuation_band("2330", metric="pe", years=1)

    # TTM EPS = 8 throughout → PE = price / 8 → 12.5, 15.0, 18.75
    values = [p["value"] for p in result["series"]]
    assert values == [pytest.approx(12.5), pytest.approx(15.0), pytest.approx(18.75)]
    assert result["stats"]["current"] == pytest.approx(18.75)
    assert result["stats"]["mean"] == pytest.approx(15.42, abs=0.05)
    assert result["metric"] == "pe"


@pytest.mark.asyncio
async def test_get_valuation_band_pe_skips_negative_eps():
    """When TTM EPS is non-positive the day's value is None (gap)."""
    bars = [
        {"time": "2023-06-30", "close": 100.0},
        {"time": "2023-12-31", "close": 100.0},
    ]
    income = [
        {"date": "2022-12-31", "type": "EPS", "value": -1.0},
        {"date": "2023-03-31", "type": "EPS", "value": -1.0},
        {"date": "2023-06-30", "type": "EPS", "value": -1.0},
        {"date": "2023-09-30", "type": "EPS", "value": 1.5},
        {"date": "2023-12-31", "type": "EPS", "value": 2.5},
    ]
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "get_history", new_callable=AsyncMock, return_value=bars), \
         patch.object(svc.finmind, "get_financials", new_callable=AsyncMock, return_value=income), \
         patch.object(svc.finmind, "get_balance_sheet", new_callable=AsyncMock, return_value=[]):
        result = await svc.get_valuation_band("9999", metric="pe", years=1)

    # 2023-06-30: TTM = -3 → gap.
    # 2023-12-31: TTM = -1 + -1 + 1.5 + 2.5 = 2 → PE = 50.
    assert result["series"][0]["value"] is None
    assert result["series"][1]["value"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_get_valuation_band_pb_uses_balance_sheet():
    bars = [{"time": "2023-12-31", "close": 200.0}]
    # NetIncome 100, EPS 2.0 → implied shares = 50.
    # Equity 5000 / 50 shares = BVPS 100. PB = 200 / 100 = 2.0
    income = [
        {"date": "2023-12-31", "type": "NetIncome", "value": 100.0},
        {"date": "2023-12-31", "type": "EPS",       "value": 2.0},
    ]
    bs = [
        {"date": "2023-12-31", "type": "Equity", "value": 5000.0},
    ]
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "get_history", new_callable=AsyncMock, return_value=bars), \
         patch.object(svc.finmind, "get_financials", new_callable=AsyncMock, return_value=income), \
         patch.object(svc.finmind, "get_balance_sheet", new_callable=AsyncMock, return_value=bs):
        result = await svc.get_valuation_band("2330", metric="pb", years=1)

    assert result["metric"] == "pb"
    assert result["series"][0]["value"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_get_valuation_band_invalid_metric_raises():
    with pytest.raises(ValueError):
        await svc.get_valuation_band("2330", metric="bogus")
