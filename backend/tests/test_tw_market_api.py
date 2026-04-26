"""
Integration tests for the TW market API endpoints.
External calls (TWSE, FinMind, MOPS, yfinance) are mocked so CI runs offline.
"""
import time
import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


# ── helpers ───────────────────────────────────────────────────────

async def _auth_headers(client: AsyncClient, email: str = "tw_user@example.com") -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass1234!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass1234!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mock_quote(symbol: str = "2330") -> dict:
    return {
        "symbol": symbol, "market": "TW", "exchange": "TWSE",
        "name_zh": "台積電", "price": 820.0, "change": 5.0, "change_pct": 0.61,
        "volume": 25_000_000, "open": 818.0, "high": 825.0, "low": 815.0,
        "currency": "TWD", "ts": int(time.time() * 1000),
        "tz": "Asia/Taipei", "is_market_open": True,
    }


def _mock_bars(n: int = 5) -> list[dict]:
    return [
        {"time": f"2024-01-{i + 1:02d}", "open": 800.0, "high": 830.0,
         "low": 795.0, "close": 820.0 + i, "volume": 20_000_000}
        for i in range(n)
    ]


def _mock_institutional_rows() -> list[dict]:
    return [
        {"date": "2024-01-15", "symbol": "2330",
         "fini_buy": 10_000_000, "fini_sell": 5_000_000,
         "sitc_buy": 1_000_000, "sitc_sell": 500_000,
         "dealer_buy": 300_000, "dealer_sell": 100_000},
        {"date": "2024-01-14", "symbol": "2330",
         "fini_buy": 8_000_000, "fini_sell": 9_000_000,
         "sitc_buy": 500_000, "sitc_sell": 800_000,
         "dealer_buy": 200_000, "dealer_sell": 250_000},
    ]


def _mock_margin_rows() -> list[dict]:
    return [
        {"date": "2024-01-15", "symbol": "2330",
         "margin_purchase": 500_000, "margin_balance": 15_000_000,
         "short_sale": 100_000, "short_balance": 2_000_000},
    ]


def _mock_revenue_rows() -> list[dict]:
    return [
        {"date": "2024-01", "symbol": "2330",
         "revenue": 200_000_000, "revenue_mom": 5.0, "revenue_yoy": 15.0},
        {"date": "2023-12", "symbol": "2330",
         "revenue": 190_000_000, "revenue_mom": 2.0, "revenue_yoy": 12.0},
    ]


# ── auth enforcement ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quote_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/quote/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_history_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/history/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_fundamentals_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/fundamentals/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_institutional_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/institutional/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_margin_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/margin/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_revenue_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/revenue/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_screener_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/screener")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_indices_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/indices")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_search_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/search?q=2330")
    assert r.status_code == 401


# ── quote ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quote_shape(client: AsyncClient):
    h = await _auth_headers(client, "tw_quote@example.com")
    with patch("services.tw_market_service.get_quote", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_quote("2330")
        r = await client.get("/api/tw/quote/2330", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "2330"
    assert data["market"] == "TW"
    assert data["exchange"] == "TWSE"
    assert data["currency"] == "TWD"
    assert data["tz"] == "Asia/Taipei"
    assert "is_market_open" in data


# ── history ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_returns_bars(client: AsyncClient):
    h = await _auth_headers(client, "tw_hist@example.com")
    bars = _mock_bars(10)
    with patch("services.tw_market_service.get_history", new_callable=AsyncMock) as mock:
        mock.return_value = bars
        r = await client.get("/api/tw/history/2330?months=6", headers=h)

    assert r.status_code == 200
    result = r.json()
    assert len(result) == 10
    for field in ("time", "open", "high", "low", "close", "volume"):
        assert field in result[0]


@pytest.mark.asyncio
async def test_history_default_months(client: AsyncClient):
    """Default months=12 should be forwarded to the service."""
    h = await _auth_headers(client, "tw_hist_default@example.com")
    with patch("services.tw_market_service.get_history", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_bars(3)
        await client.get("/api/tw/history/2330", headers=h)

    mock.assert_awaited_once_with("2330", months=12)


@pytest.mark.asyncio
async def test_history_months_out_of_range(client: AsyncClient):
    """months=0 must fail query validation (ge=1)."""
    h = await _auth_headers(client, "tw_hist_bad@example.com")
    r = await client.get("/api/tw/history/2330?months=0", headers=h)
    assert r.status_code == 422


# ── fundamentals ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fundamentals_returns_data(client: AsyncClient):
    h = await _auth_headers(client, "tw_fund@example.com")
    payload = {
        "symbol": "2330", "market": "TW", "pe_ratio": 20.5,
        "pb_ratio": 6.1, "dividend_yield": 0.022,
        "fetched_at": "2024-01-15T12:00:00Z",
    }
    with patch("services.tw_market_service.get_fundamentals", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        r = await client.get("/api/tw/fundamentals/2330", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "2330"
    assert data["pe_ratio"] == pytest.approx(20.5)


# ── institutional ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_institutional_returns_rows(client: AsyncClient):
    h = await _auth_headers(client, "tw_inst@example.com")
    with patch("services.tw_market_service.get_institutional", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_institutional_rows()
        r = await client.get("/api/tw/institutional/2330?days=30", headers=h)

    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert rows[0]["fini_buy"] == 10_000_000


@pytest.mark.asyncio
async def test_institutional_days_param_forwarded(client: AsyncClient):
    h = await _auth_headers(client, "tw_inst_days@example.com")
    with patch("services.tw_market_service.get_institutional", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get("/api/tw/institutional/2330?days=60", headers=h)

    mock.assert_awaited_once_with("2330", days=60)


# ── margin ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_margin_returns_rows(client: AsyncClient):
    h = await _auth_headers(client, "tw_margin@example.com")
    with patch("services.tw_market_service.get_margin", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_margin_rows()
        r = await client.get("/api/tw/margin/2330", headers=h)

    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["margin_balance"] == 15_000_000


# ── revenue ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revenue_returns_rows(client: AsyncClient):
    h = await _auth_headers(client, "tw_rev@example.com")
    with patch("services.tw_market_service.get_revenue", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_revenue_rows()
        r = await client.get("/api/tw/revenue/2330?months=12", headers=h)

    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert rows[0]["revenue_yoy"] == pytest.approx(15.0)


# ── screener ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_screener_returns_list(client: AsyncClient):
    h = await _auth_headers(client, "tw_scr@example.com")
    items = [
        {"symbol": "2330", "market": "TW", "exchange": "TWSE",
         "name_zh": "台積電", "price": 820.0, "volume": 25_000_000},
        {"symbol": "2317", "market": "TW", "exchange": "TWSE",
         "name_zh": "鴻海", "price": 105.0, "volume": 15_000_000},
    ]
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = items
        r = await client.get("/api/tw/screener", headers=h)

    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_screener_forwards_filters(client: AsyncClient):
    h = await _auth_headers(client, "tw_scr_filter@example.com")
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get(
            "/api/tw/screener?exchange=TPEx&min_volume=1000000&limit=50",
            headers=h,
        )

    _, kwargs = mock.call_args
    assert kwargs["exchange"] == "TPEx"
    assert kwargs["min_volume"] == 1_000_000
    assert kwargs["limit"] == 50


@pytest.mark.asyncio
async def test_screener_include_etf_default_true(client: AsyncClient):
    h = await _auth_headers(client, "tw_scr_etf_default@example.com")
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get("/api/tw/screener", headers=h)

    _, kwargs = mock.call_args
    assert kwargs["include_etf"] is True


@pytest.mark.asyncio
async def test_screener_include_etf_false_forwards(client: AsyncClient):
    h = await _auth_headers(client, "tw_scr_etf_off@example.com")
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get("/api/tw/screener?include_etf=false", headers=h)

    _, kwargs = mock.call_args
    assert kwargs["include_etf"] is False


@pytest.mark.asyncio
async def test_screener_etf_only_default_false(client: AsyncClient):
    h = await _auth_headers(client, "tw_scr_etf_only_default@example.com")
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get("/api/tw/screener", headers=h)

    _, kwargs = mock.call_args
    assert kwargs["etf_only"] is False


@pytest.mark.asyncio
async def test_screener_etf_only_forwards(client: AsyncClient):
    h = await _auth_headers(client, "tw_scr_etf_only@example.com")
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get("/api/tw/screener?etf_only=true", headers=h)

    _, kwargs = mock.call_args
    assert kwargs["etf_only"] is True


@pytest.mark.asyncio
async def test_screener_forwards_fundamental_filters(client: AsyncClient):
    """PE/PB/yield filters from query string reach the service kwargs."""
    h = await _auth_headers(client, "tw_scr_fund@example.com")
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get(
            "/api/tw/screener?min_pe=5&max_pe=20&min_pb=0.5&max_pb=3"
            "&min_dividend_yield=2.5",
            headers=h,
        )

    _, kwargs = mock.call_args
    assert kwargs["min_pe"] == pytest.approx(5.0)
    assert kwargs["max_pe"] == pytest.approx(20.0)
    assert kwargs["min_pb"] == pytest.approx(0.5)
    assert kwargs["max_pb"] == pytest.approx(3.0)
    assert kwargs["min_dividend_yield"] == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_screener_returns_fundamental_fields(client: AsyncClient):
    """Response shape carries pe_ratio / pb_ratio / dividend_yield / change_pct."""
    h = await _auth_headers(client, "tw_scr_shape@example.com")
    items = [
        {
            "symbol": "2330", "market": "TW", "exchange": "TWSE",
            "name_zh": "台積電", "price": 820.0,
            "change": 5.0, "change_pct": 0.61, "volume": 25_000_000,
            "pe_ratio": 20.5, "pb_ratio": 6.1, "dividend_yield": 2.1,
        },
    ]
    with patch("services.tw_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = items
        r = await client.get("/api/tw/screener?max_pe=30", headers=h)

    assert r.status_code == 200
    row = r.json()[0]
    assert row["pe_ratio"] == pytest.approx(20.5)
    assert row["pb_ratio"] == pytest.approx(6.1)
    assert row["dividend_yield"] == pytest.approx(2.1)
    assert row["change_pct"] == pytest.approx(0.61)


# ── indices ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_indices_shape(client: AsyncClient):
    h = await _auth_headers(client, "tw_idx@example.com")
    with patch("services.tw_market_service.get_index", new_callable=AsyncMock) as mock:
        mock.return_value = {
            "index": "TAIEX", "value": 17500.0, "change": 120.5,
            "time": "2024-01-15T13:30:00+08:00",
        }
        r = await client.get("/api/tw/indices", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["index"] == "TAIEX"
    assert data["value"] == pytest.approx(17500.0)


# ── news ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_news_returns_list(client: AsyncClient):
    h = await _auth_headers(client, "tw_news@example.com")
    items = [
        {"title": "台積電Q4法說", "url": "https://example.com/1",
         "published_at": "2024-01-15T10:00:00Z", "source": "經濟日報"},
    ]
    with patch("services.tw_market_service.get_news", new_callable=AsyncMock) as mock:
        mock.return_value = items
        r = await client.get("/api/tw/news/2330?limit=5", headers=h)

    assert r.status_code == 200
    assert len(r.json()) == 1


@pytest.mark.asyncio
async def test_news_empty_list(client: AsyncClient):
    h = await _auth_headers(client, "tw_news_empty@example.com")
    with patch("services.tw_market_service.get_news", new_callable=AsyncMock) as mock:
        mock.return_value = []
        r = await client.get("/api/tw/news/9999", headers=h)

    assert r.status_code == 200
    assert r.json() == []


# ── search ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_matches(client: AsyncClient):
    h = await _auth_headers(client, "tw_search@example.com")
    with patch.object(
        __import__("services.tw_market_service", fromlist=["_exchange_map"]),
        "_exchange_map",
        {"2330": "TWSE", "2317": "TWSE", "2454": "TWSE", "3008": "TWSE"},
    ):
        r = await client.get("/api/tw/search?q=23", headers=h)

    assert r.status_code == 200
    symbols = [item["symbol"] for item in r.json()]
    assert "2330" in symbols
    assert "2317" in symbols
    assert "3008" not in symbols


@pytest.mark.asyncio
async def test_search_empty_query_rejected(client: AsyncClient):
    h = await _auth_headers(client, "tw_search_empty@example.com")
    r = await client.get("/api/tw/search?q=", headers=h)
    assert r.status_code == 422


# ── financial health ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/health/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_health_response_shape(client: AsyncClient):
    """Endpoint forwards `periods` and surfaces summary + lights."""
    h = await _auth_headers(client, "tw_health@example.com")
    payload = {
        "symbol": "2330",
        "market": "TW",
        "periods": [
            {"date": "2023-12-31", "revenue": 625529.0, "net_income": 230000.0,
             "eps": 8.84, "gross_margin": 53.1, "operating_margin": 41.9,
             "net_margin": 36.8, "debt_ratio": 32.0, "current_ratio": 2.5,
             "operating_cf": 400000.0, "free_cf": 200000.0,
             "total_equity": 2_500_000.0},
        ],
        "summary": {
            "latest_roe": 28.5, "latest_debt_ratio": 32.0,
            "latest_gross_margin": 53.1, "latest_net_margin": 36.8,
            "revenue_yoy": 12.0, "cf_positive_streak_4q": 4,
        },
        "lights": {
            "profitability": "green", "safety": "green",
            "growth": "green", "cash_flow": "green",
        },
    }
    with patch("services.tw_market_service.get_health", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        r = await client.get("/api/tw/health/2330?periods=4", headers=h)

    assert r.status_code == 200
    body = r.json()
    assert body["lights"]["profitability"] == "green"
    assert body["summary"]["latest_roe"] == pytest.approx(28.5)
    assert body["periods"][0]["gross_margin"] == pytest.approx(53.1)
    mock.assert_awaited_once_with("2330", periods=4)


@pytest.mark.asyncio
async def test_health_periods_param_validated(client: AsyncClient):
    h = await _auth_headers(client, "tw_health_bad@example.com")
    r = await client.get("/api/tw/health/2330?periods=0", headers=h)
    assert r.status_code == 422


# ── valuation band ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valuation_band_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/valuation-band/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valuation_band_response_shape(client: AsyncClient):
    h = await _auth_headers(client, "tw_band@example.com")
    payload = {
        "symbol": "2330",
        "metric": "pe",
        "series": [
            {"date": "2023-12-31", "value": 18.5},
            {"date": "2024-01-02", "value": 18.8},
        ],
        "stats": {
            "mean": 18.5, "std": 1.0,
            "min": 17.0, "max": 20.0,
            "p10": 17.2, "p25": 17.8, "p50": 18.5, "p75": 19.2, "p90": 19.8,
            "current": 18.8, "current_z": 0.3,
        },
    }
    with patch("services.tw_market_service.get_valuation_band", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        r = await client.get("/api/tw/valuation-band/2330?metric=pe&years=3", headers=h)

    assert r.status_code == 200
    body = r.json()
    assert body["metric"] == "pe"
    assert body["stats"]["current"] == pytest.approx(18.8)
    mock.assert_awaited_once_with("2330", metric="pe", years=3)


@pytest.mark.asyncio
async def test_valuation_band_rejects_bad_metric(client: AsyncClient):
    h = await _auth_headers(client, "tw_band_bad@example.com")
    r = await client.get("/api/tw/valuation-band/2330?metric=bogus", headers=h)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_valuation_band_rejects_years_out_of_range(client: AsyncClient):
    h = await _auth_headers(client, "tw_band_yrs@example.com")
    r = await client.get("/api/tw/valuation-band/2330?years=11", headers=h)
    assert r.status_code == 422


# ── dividends + ETF holdings ──────────────────────────────────────

@pytest.mark.asyncio
async def test_dividends_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/dividends/2330")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_dividends_returns_list(client: AsyncClient):
    h = await _auth_headers(client, "tw_div@example.com")
    payload = [
        {"date": "2023-08-15", "ex_date": "2023-09-15",
         "cash_dividend": 11.0, "stock_dividend": 0.0},
    ]
    with patch("services.tw_market_service.get_dividends", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        r = await client.get("/api/tw/dividends/2330", headers=h)

    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["cash_dividend"] == pytest.approx(11.0)


@pytest.mark.asyncio
async def test_etf_holdings_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/etf/0050/holdings")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_etf_holdings_response_shape(client: AsyncClient):
    h = await _auth_headers(client, "tw_etf@example.com")
    payload = {
        "as_of": "2024-01-31",
        "holdings": [
            {"symbol": "2330", "name_zh": "台積電", "weight": 25.0},
            {"symbol": "2454", "name_zh": "聯發科", "weight": 10.0},
        ],
    }
    with patch("services.tw_market_service.get_etf_holdings", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        r = await client.get("/api/tw/etf/0050/holdings", headers=h)

    assert r.status_code == 200
    body = r.json()
    assert body["as_of"] == "2024-01-31"
    assert body["holdings"][0]["symbol"] == "2330"
    mock.assert_awaited_once_with("0050")
