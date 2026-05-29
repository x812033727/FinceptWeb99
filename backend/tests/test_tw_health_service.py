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

    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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

    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    # 6 quarterly EPS rows so every bar date has a full 4-quarter TTM
    # available. Without the earliest row, 2023-06-30 would only see 3
    # quarters and PE would be 100/6 ≈ 16.67 instead of 100/8 = 12.5.
    income = [
        {"date": "2022-09-30", "type": "EPS", "value": 2.0},
        {"date": "2022-12-31", "type": "EPS", "value": 2.0},
        {"date": "2023-03-31", "type": "EPS", "value": 2.0},
        {"date": "2023-06-30", "type": "EPS", "value": 2.0},
        {"date": "2023-09-30", "type": "EPS", "value": 2.0},
        {"date": "2023-12-31", "type": "EPS", "value": 2.0},
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
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


# ── ETF detection + dividends + holdings ────────────────────────


def test_is_etf_recognizes_4_to_6_digit_codes_starting_with_00():
    # Standard 4-digit ETFs
    assert svc.is_etf("0050") is True
    assert svc.is_etf("0056") is True
    # 5-digit
    assert svc.is_etf("00713") is True
    # 6-digit
    assert svc.is_etf("006208") is True
    # Inverse / leveraged ETFs (suffix)
    assert svc.is_etf("00632R") is True


def test_is_etf_rejects_regular_stocks_and_garbage():
    assert svc.is_etf("2330") is False
    assert svc.is_etf("1101") is False
    assert svc.is_etf("9999") is False
    assert svc.is_etf("") is False
    assert svc.is_etf("AAPL") is False
    # 3-digit codes shouldn't match
    assert svc.is_etf("005") is False


@pytest.mark.asyncio
async def test_get_dividends_normalizes_and_sorts():
    """Cash + stock components summed, rows without payout dropped."""
    raw = [
        {"date": "2023-08-15", "CashEarningsDistribution": 2.5,
         "CashStatutorySurplus": 0.5, "StockEarningsDistribution": 0,
         "CashExDividendTradingDate": "2023-09-15"},
        {"date": "2022-08-15", "CashEarningsDistribution": 2.0,
         "StockEarningsDistribution": 0.1,
         "CashExDividendTradingDate": "2022-09-15"},
        # Empty payout row — should be dropped.
        {"date": "2021-01-01", "CashEarningsDistribution": 0,
         "StockEarningsDistribution": 0},
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_dividends", new_callable=AsyncMock, return_value=raw):
        result = await svc.get_dividends("2330")

    assert len(result) == 2
    # Sorted ascending
    assert result[0]["date"] == "2022-08-15"
    assert result[1]["date"] == "2023-08-15"
    # Cash summed = 2.5 + 0.5 = 3.0
    assert result[1]["cash_dividend"] == pytest.approx(3.0)
    assert result[0]["stock_dividend"] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_get_etf_holdings_returns_empty_for_non_etf():
    """Regular stock symbols short-circuit to empty without hitting FinMind."""
    with patch.object(svc.finmind, "get_etf_holdings", new_callable=AsyncMock) as mock:
        result = await svc.get_etf_holdings("2330")

    assert result == {"as_of": None, "holdings": []}
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_etf_holdings_picks_latest_snapshot_and_sorts():
    raw = [
        {"date": "2024-01-31", "stock_id": "2330", "stock_name": "台積電", "weight": 25.0},
        {"date": "2024-01-31", "stock_id": "2317", "stock_name": "鴻海", "weight": 5.0},
        {"date": "2024-01-31", "stock_id": "2454", "stock_name": "聯發科", "weight": 10.0},
        # Older snapshot — should be ignored.
        {"date": "2023-12-31", "stock_id": "2330", "stock_name": "台積電", "weight": 22.0},
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.finmind, "get_etf_holdings", new_callable=AsyncMock, return_value=raw):
        result = await svc.get_etf_holdings("0050")

    assert result["as_of"] == "2024-01-31"
    weights = [h["weight"] for h in result["holdings"]]
    # Sorted descending by weight
    assert weights == [25.0, 10.0, 5.0]
    assert result["holdings"][0]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_etf_holdings_empty_when_finmind_returns_nothing():
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc.finmind, "get_etf_holdings", new_callable=AsyncMock, return_value=[]):
        result = await svc.get_etf_holdings("00713")

    assert result == {"as_of": None, "holdings": []}


def test_normalize_quote_flags_etf():
    """ETF symbols get is_etf=True, regular stocks get is_etf=False."""
    raw = {"close": 100.0, "name_zh": "元大台灣高息低波"}
    q = svc._normalize_quote("00713", raw)
    assert q["is_etf"] is True

    q2 = svc._normalize_quote("2330", raw)
    assert q2["is_etf"] is False


# ── Screener volume-sort + ETF exclusion ────────────────────────


def _stock_row(code: str, vol: int, name: str = "", price: float = 100.0) -> dict:
    return {
        "Code": code, "Name": name,
        "ClosingPrice": str(price),
        "TradeVolume": str(vol),
        "Change": "0",
    }


@pytest.mark.asyncio
async def test_screener_sorts_by_volume_desc():
    """Most-traded names appear first regardless of code order."""
    stocks = [
        _stock_row("0050", vol=10_000_000, name="元大台灣50"),
        _stock_row("2330", vol=50_000_000, name="台積電"),
        _stock_row("00713", vol=5_000_000, name="高息低波"),
        _stock_row("2317", vol=20_000_000, name="鴻海"),
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_all_twse_symbols", new_callable=AsyncMock, return_value=stocks):
        result = await svc.get_screener(limit=10)

    assert [r["symbol"] for r in result] == ["2330", "2317", "0050", "00713"]


@pytest.mark.asyncio
async def test_screener_excludes_etf_when_include_etf_false():
    """include_etf=False drops 00xxx symbols from the results."""
    stocks = [
        _stock_row("0050", vol=10_000_000, name="元大台灣50"),
        _stock_row("2330", vol=50_000_000, name="台積電"),
        _stock_row("00713", vol=5_000_000, name="高息低波"),
        _stock_row("1101", vol=3_000_000, name="台泥"),
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_all_twse_symbols", new_callable=AsyncMock, return_value=stocks):
        result = await svc.get_screener(include_etf=False, limit=10)

    symbols = [r["symbol"] for r in result]
    assert "0050" not in symbols
    assert "00713" not in symbols
    assert "2330" in symbols
    assert "1101" in symbols


@pytest.mark.asyncio
async def test_screener_etf_only_keeps_only_etfs():
    """etf_only=True drops every regular stock."""
    stocks = [
        _stock_row("0050", vol=10_000_000, name="元大台灣50"),
        _stock_row("2330", vol=50_000_000, name="台積電"),
        _stock_row("00713", vol=5_000_000, name="高息低波"),
        _stock_row("1101", vol=3_000_000, name="台泥"),
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_all_twse_symbols", new_callable=AsyncMock, return_value=stocks):
        result = await svc.get_screener(etf_only=True, limit=10)

    symbols = [r["symbol"] for r in result]
    assert symbols == ["0050", "00713"]   # ordered by volume desc


@pytest.mark.asyncio
async def test_screener_volume_sort_lifts_regular_stock_above_etf_glut():
    """Regression: 250 low-volume ETFs no longer bury a high-volume stock."""
    stocks = [
        _stock_row(f"00{900 + i:04d}"[:5], vol=100, name=f"ETF{i}")
        for i in range(250)
    ] + [
        _stock_row("2330", vol=50_000_000, name="台積電"),
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc.twse, "get_all_twse_symbols", new_callable=AsyncMock, return_value=stocks):
        result = await svc.get_screener(limit=2)

    assert result[0]["symbol"] == "2330"


# ── News pipeline (Google News RSS primary) ─────────────────────


@pytest.mark.asyncio
async def test_get_news_uses_google_when_yfinance_empty():
    """Google News returns hits → yfinance fallback never called."""
    google_items = [
        {"title": "台積電 Q4 法說", "publisher": "經濟日報",
         "link": "https://news.google.com/articles/abc",
         "published_at": "2026-04-26T10:00:00+00:00", "thumbnail": None},
    ]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new_callable=AsyncMock, return_value=google_items) as gmock, \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=[]) as yf_mock:
        result = await svc.get_news("2330", limit=5)

    assert result == google_items
    gmock.assert_awaited_once()
    yf_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_news_falls_back_to_yfinance_when_google_empty():
    """Google empty → fall back to yfinance."""
    yf_items = [{"title": "via yfinance", "publisher": "yf",
                 "link": "https://example.com", "published_at": "",
                 "thumbnail": None}]
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=yf_items):
        result = await svc.get_news("2330", limit=5)

    assert result == yf_items


@pytest.mark.asyncio
async def test_get_news_uses_cached_name_in_query():
    """When a recent quote is cached, the news query includes the Chinese name."""
    quote_cached = {"name_zh": "台積電", "price": 2185.0}
    captured: dict = {}

    async def _spy(query: str, limit: int = 10):
        captured["query"] = query
        return []

    with patch.object(svc, "cache_get_json", new_callable=AsyncMock,
                      side_effect=[None, quote_cached]), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock), \
         patch.object(svc, "_google_news_rss", new=_spy), \
         patch.object(svc, "_yfinance_news_fallback", new_callable=AsyncMock, return_value=[]):
        await svc.get_news("2330", limit=5)

    assert "2330" in captured["query"]
    assert "台積電" in captured["query"]


@pytest.mark.asyncio
async def test_google_news_rss_parses_xml_correctly():
    """Verify the RSS parser extracts title/link/pubDate/source."""
    sample = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <item>
          <title>台積電 Q4 法說會重點</title>
          <link>https://news.google.com/articles/abc123</link>
          <pubDate>Sat, 26 Apr 2026 10:00:00 GMT</pubDate>
          <source url="https://money.udn.com">經濟日報</source>
        </item>
        <item>
          <title>台積電董事會通過配息</title>
          <link>https://news.google.com/articles/def456</link>
          <pubDate>Fri, 25 Apr 2026 08:00:00 GMT</pubDate>
          <source url="https://www.cnyes.com">鉅亨網</source>
        </item>
      </channel>
    </rss>"""

    class _MockResponse:
        text = sample
        def raise_for_status(self):
            return None

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, *_, **__): return _MockResponse()

    with patch("httpx.AsyncClient", return_value=_MockClient()):
        items = await svc._google_news_rss("2330 台積電", limit=10)

    assert len(items) == 2
    assert items[0]["title"] == "台積電 Q4 法說會重點"
    assert items[0]["publisher"] == "經濟日報"
    assert items[0]["link"].startswith("https://news.google.com/")
    assert "2026-04-26" in items[0]["published_at"]
    assert items[1]["publisher"] == "鉅亨網"


@pytest.mark.asyncio
async def test_google_news_rss_respects_limit():
    sample_items = "".join(
        f"<item><title>News {i}</title><link>https://x/{i}</link>"
        f"<pubDate>Sat, 26 Apr 2026 10:00:00 GMT</pubDate></item>"
        for i in range(20)
    )
    sample = f"<?xml version='1.0'?><rss><channel>{sample_items}</channel></rss>"

    class _MockResponse:
        text = sample
        def raise_for_status(self): return None

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, *_, **__): return _MockResponse()

    with patch("httpx.AsyncClient", return_value=_MockClient()):
        items = await svc._google_news_rss("test", limit=5)

    assert len(items) == 5
