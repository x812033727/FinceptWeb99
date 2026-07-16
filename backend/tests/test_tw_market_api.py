"""
Integration tests for the TW market API endpoints.
External calls (TWSE, FinMind, MOPS, yfinance) are mocked so CI runs offline.
"""
import time
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

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
        "data_source": "twse_mis",
    }


def _mock_bars(n: int = 5) -> list[dict]:
    return [
        {"time": f"2024-01-{i + 1:02d}", "open": 800.0, "high": 830.0,
         "low": 795.0, "close": 820.0 + i, "volume": 20_000_000,
         "data_source": "ohlcv_daily"}
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
async def test_security_master_requires_auth(client: AsyncClient):
    r = await client.get("/api/tw/security-master/00980B")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_security_master_exposes_effective_tax_rule_and_source(client: AsyncClient):
    headers = await _auth_headers(client, "security_master@example.com")
    r = await client.get(
        "/api/tw/security-master/00980B?as_of=2026-07-15",
        headers=headers,
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["instrument_type"] == "etf_bond"
    assert payload["sell_tax_bps"] == 0
    assert payload["effective_from"] == "2026-07-15"
    assert payload["source"] == "runtime_fallback"
    assert payload["tax_source_url"].startswith("https://www.etax.nat.gov.tw/")


@pytest.mark.asyncio
async def test_security_master_sync_is_admin_only(client: AsyncClient):
    headers = await _auth_headers(client, "security_sync_viewer@example.com")
    r = await client.post("/api/tw/security-master/sync", headers=headers)
    assert r.status_code == 403


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
    assert data["meta"]["freshness"] == "fresh"
    assert data["meta"]["fallback_chain"][0] == "twse_mis"


@pytest.mark.asyncio
async def test_quote_verify_surfaces_verified_sources(client: AsyncClient):
    h = await _auth_headers(client, "tw_quote_verify@example.com")
    check = {
        "status": "verified", "primary_source": "twse",
        "secondary_source": "finmind", "spread_pct": 0.08,
        "observations": {"twse": 100, "finmind": 100.08},
        "checked_at": "2026-07-15T00:00:00+00:00", "flags": [],
    }
    with patch("services.tw_market_service.get_quote", new=AsyncMock(return_value=_mock_quote("2330"))), \
         patch("services.tw_market_service.verify_quote_consistency", new=AsyncMock(return_value=check)) as verify:
        response = await client.get("/api/tw/quote/2330?verify=true", headers=h)

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["consistency"] == "verified"
    assert meta["cross_checked_sources"] == ["twse", "finmind"]
    verify.assert_awaited_once()


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
    assert result[0]["meta"]["freshness"] == "stale"


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
    assert data["meta"]["freshness"] == "stale"


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


@pytest.mark.asyncio
async def test_news_recent_reads_market_wide_with_sentiment(client: AsyncClient):
    """`/tw/news/recent` powers the dashboard card. Must hit the DB-only
    path (not the live RSS waterfall) and forward sentiment fields."""
    h = await _auth_headers(client, "tw_news_recent@example.com")
    items = [
        {
            "title": "台股盤後 - 加權收漲",
            "publisher": "鉅亨網",
            "link": "https://example.com/a",
            "published_at": "2026-04-30T05:00:00+00:00",
            "thumbnail": None,
            "data_source": "google_news_tw",
            "sentiment_score": 0.42,
            "sentiment_label": "bullish",
        },
    ]
    with patch(
        "services.ingest.repository.read_recent_news_autosession",
        new_callable=AsyncMock,
    ) as mock_read:
        mock_read.return_value = items
        r = await client.get("/api/tw/news/recent?limit=20", headers=h)

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["sentiment_label"] == "bullish"
    # Pin the call shape so the dashboard's "market-wide news only"
    # contract isn't accidentally widened to per-symbol later.
    kwargs = mock_read.await_args.kwargs
    assert kwargs["symbol"] is None
    assert kwargs["include_sentiment"] is True


@pytest.mark.asyncio
async def test_news_recent_clamps_limit(client: AsyncClient):
    """Query string must enforce limit ≤ 50 (FastAPI Query bound)."""
    h = await _auth_headers(client, "tw_news_recent_clamp@example.com")
    r = await client.get("/api/tw/news/recent?limit=999", headers=h)
    assert r.status_code == 422


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


def _factor_quality(status: str = "good") -> dict:
    return {
        "status": status, "flags": ["unadjusted_price_history"],
        "sources": ["fundamentals_snapshots", "ohlcv_daily", "tw_company_info"],
        "universe_size": 100, "eligible_count": 80, "returned_count": 1,
        "momentum_coverage_pct": 90, "stale_fundamentals_excluded": 0,
    }


@pytest.mark.asyncio
async def test_factor_ranking_requires_auth(client: AsyncClient):
    response = await client.get("/api/tw/factor-ranking")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_factor_ranking_response_and_query_forwarding(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor@example.com")
    payload = {
        "market": "TW", "as_of": "2025-06-30", "profile": "value",
        "methodology_version": "tw-explainable-multifactor-v8",
        "sector_neutral": False,
        "weights": {"value": .45, "quality": .15, "momentum": .1,
                    "low_volatility": .1, "income": .1, "liquidity": .1},
        "candidates": [{
            "rank": 1, "symbol": "2330", "name_zh": "台積電", "industry": "半導體",
            "price": 1000, "price_session": "2025-06-30", "fundamentals_as_of": "2025-06-27",
            "score": 100, "composite_z": 1.5, "raw_composite_z": 1.8,
            "sector_adjustment": .3, "factor_coverage": 1,
            "missing_factors": [],
            "factors": {name: {"raw": 1, "z": 1} for name in
                        ("value", "quality", "momentum", "low_volatility", "income", "liquidity")},
        }],
        "quality": _factor_quality(),
        "methodology": {"model": "deterministic ranking; not machine learning"},
    }
    with patch("services.tw_factor_service.get_factor_ranking", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        response = await client.get(
            "/api/tw/factor-ranking?as_of=2025-06-30&profile=value&limit=25"
            "&sector_neutral=false", headers=h,
        )

    assert response.status_code == 200
    assert response.json()["candidates"][0]["factors"]["value"]["z"] == 1
    mock.assert_awaited_once_with(
        as_of=date(2025, 6, 30), profile="value", limit=25,
        sector_neutral=False,
    )


@pytest.mark.asyncio
async def test_factor_ranking_rejects_unknown_profile(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor_bad@example.com")
    response = await client.get("/api/tw/factor-ranking?profile=magic", headers=h)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_factor_portfolio_forwards_constraints_and_returns_diagnostics(
    client: AsyncClient,
):
    h = await _auth_headers(client, "tw_factor_portfolio@example.com")
    ranking = {
        "market": "TW", "as_of": "2025-06-30", "profile": "balanced",
        "methodology_version": "tw-explainable-multifactor-v8",
        "weights": {"value": .25}, "candidates": [{"symbol": "2330"}],
        "quality": _factor_quality(), "methodology": {}, "sector_neutral": True,
        "weight_source": "profile", "model_id": None,
    }
    portfolio = {
        "market": "TW", "as_of": "2025-06-30", "profile": "balanced",
        "methodology_version": "tw-factor-portfolio-v1",
        "factor_methodology_version": "tw-explainable-multifactor-v8",
        "weight_source": "profile", "model_id": None, "converged": True,
        "solver_message": "Optimization terminated successfully",
        "positions": [{
            "symbol": "2330", "name_zh": "台積電", "industry": "半導體",
            "weight": .1, "notional_twd": 1_000_000, "factor_score": 99,
            "liquidity_cap": .1, "average_daily_value_twd": 10_000_000_000,
            "risk_contribution": .2,
        }],
        "summary": {"invested_weight": .8, "cash_weight": .2,
                    "annual_volatility": .15, "tracking_error": .08,
                    "turnover": 0, "weighted_factor_score": 75},
        "sector_weights": {"半導體": .3},
        "constraints": [{"name": "target_volatility", "actual": .15,
                         "limit": .2, "operator": "<=", "passed": True,
                         "binding": False}],
        "quality": {"status": "good", "flags": [],
                    "requested_candidate_count": 30, "eligible_candidate_count": 25,
                    "return_observations": 252, "excluded": [],
                    "benchmark": "taiex_total_return",
                    "adjusted_price_history_used": True,
                    "adjusted_price_coverage_pct": 100},
        "methodology": {"objective": "factor utility"},
    }
    with patch(
        "services.tw_factor_service.get_factor_ranking",
        new_callable=AsyncMock, return_value=ranking,
    ) as rank_mock, patch(
        "services.tw_factor_portfolio_service.construct_factor_portfolio",
        new_callable=AsyncMock, return_value=portfolio,
    ) as portfolio_mock:
        response = await client.post("/api/tw/factor-portfolio", json={
            "as_of": "2025-06-30", "candidate_count": 30,
            "max_position_weight": .1, "max_sector_weight": .3,
            "target_volatility": .2, "max_tracking_error": .12,
        }, headers=h)
    assert response.status_code == 200
    assert response.json()["positions"][0]["weight"] == pytest.approx(.1)
    rank_mock.assert_awaited_once_with(
        as_of=date(2025, 6, 30), profile="balanced", limit=30,
        sector_neutral=True,
    )
    call = portfolio_mock.await_args.kwargs
    assert call["max_position_weight"] == pytest.approx(.1)
    assert call["max_sector_weight"] == pytest.approx(.3)
    assert call["target_volatility"] == pytest.approx(.2)
    assert call["max_tracking_error"] == pytest.approx(.12)


@pytest.mark.asyncio
async def test_factor_rebalance_preview_is_owner_scoped_and_never_executes(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor_rebalance@example.com")
    portfolio_id = "11111111-1111-4111-8111-111111111111"
    ranking = {
        "market": "TW", "as_of": "2025-06-30", "profile": "balanced",
        "methodology_version": "tw-explainable-multifactor-v8",
        "weights": {"value": .25}, "candidates": [],
        "quality": _factor_quality(), "methodology": {}, "sector_neutral": True,
    }
    target = {
        "market": "TW", "as_of": "2025-06-30", "profile": "balanced",
        "methodology_version": "tw-factor-portfolio-v1",
        "factor_methodology_version": "tw-explainable-multifactor-v8",
        "weight_source": "profile", "model_id": None, "converged": True,
        "solver_message": "ok", "positions": [],
        "summary": {}, "risk_comparison": {}, "sector_weights": {},
        "constraints": [], "quality": {
            "status": "good", "flags": [], "requested_candidate_count": 30,
            "eligible_candidate_count": 20, "return_observations": 252,
            "excluded": [], "benchmark": "taiex_total_return",
            "adjusted_price_history_used": True, "adjusted_price_coverage_pct": 100,
        }, "methodology": {},
    }
    preview = {
        "portfolio_id": portfolio_id, "currency": "TWD", "portfolio_name": "核心",
        "portfolio_base_currency": "TWD", "portfolio_notional_twd": 200_000,
        "target_portfolio": target, "trades": [], "post_positions": [],
        "cost_scenarios": [], "frozen": [], "excluded": [],
        "summary": {"funded": True}, "quality_flags": [], "methodology": {},
        "preview_only": True,
    }
    with patch(
        "services.tw_factor_service.get_factor_ranking",
        new_callable=AsyncMock, return_value=ranking,
    ), patch(
        "services.tw_factor_rebalance_service.build_factor_rebalance_preview",
        new_callable=AsyncMock, return_value=preview,
    ) as preview_mock:
        response = await client.post("/api/tw/factor-portfolio/rebalance-preview", json={
            "portfolio_id": portfolio_id,
            "weight_source": "profile", "additional_cash_twd": 100_000,
            "allow_odd_lot": False,
        }, headers=h)
    assert response.status_code == 200
    assert response.json()["preview_only"] is True
    call = preview_mock.await_args.kwargs
    assert call["portfolio_id"] == portfolio_id
    assert call["additional_cash_twd"] == 100_000
    assert call["allow_odd_lot"] is False
    assert call["user_id"]


@pytest.mark.asyncio
async def test_factor_rebalance_preview_rejects_historical_date(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor_rebalance_history@example.com")
    response = await client.post("/api/tw/factor-portfolio/rebalance-preview", json={
        "portfolio_id": "11111111-1111-4111-8111-111111111111",
        "as_of": "2020-01-01", "weight_source": "profile",
    }, headers=h)
    assert response.status_code == 400
    assert "historical holdings" in response.json()["detail"]


@pytest.mark.asyncio
async def test_factor_validation_response_and_cost_forwarding(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor_validation@example.com")
    payload = {
        "market": "TW", "profile": "balanced",
        "methodology_version": "tw-explainable-multifactor-v8",
        "sector_neutral": True,
        "start_date": "2024-01-01", "end_date": "2025-01-01",
        "top_n": 20, "holding_sessions": 21, "transaction_cost_bps": 25,
        "portfolio_notional_twd": 10_000_000,
        "max_participation_rate": .05, "impact_coefficient_bps": 10,
        "benchmark_requested": "taiex_total_return",
        "benchmark_used": "taiex_total_return",
        "weight_mode": "walk_forward",
        "periods": [{
            "anchor": "2024-06-03", "holdings": ["2330"], "holding_count": 20,
            "turnover": 1, "gross_return_pct": 3, "cost_pct": .25,
            "net_return_pct": 2.75, "benchmark_return_pct": 1,
            "excess_return_pct": 1.75, "quality_status": "good",
        }],
        "summary": {"period_count": 1, "cumulative_return_pct": 2.75},
        "regime_analysis": {"bull": {"period_count": 1}},
        "factor_diagnostics": {
            "composite": {"period_count": 1, "average_rank_ic": .1},
        },
        "factor_correlation_matrix": {"value": {"value": 1}},
        "quantile_analysis": {
            "period_count": 1, "average_returns_pct": [0, .5, 1, 1.5, 2],
            "average_top_bottom_spread_pct": 2,
        },
        "sensitivity_analysis": {
            "holding_sessions": {"21": {"period_count": 1, "average_rank_ic": .1}},
            "top_n": {"20": {"period_count": 1, "average_forward_return_pct": 2}},
        },
        "factor_decay_analysis": {
            "composite": {
                "average_rank_ic_by_horizon": {"5": .05, "21": .1, "63": .04},
                "peak_absolute_ic_horizon": 21,
                "direction_consistent": True,
            },
        },
        "weight_stability": {
            "mode": "walk_forward",
            "base_weights": {"value": .2, "quality": .2, "momentum": .2,
                             "low_volatility": .15, "income": .1, "liquidity": .15},
            "adaptive_period_count": 1, "fallback_period_count": 0,
            "average_weight_turnover_pct": 2.5,
            "maximum_weight_turnover_pct": 2.5,
            "factor_ranges": {
                "value": {"minimum": .19, "maximum": .21, "latest": .21},
            },
        },
        "quality": _factor_quality("degraded"),
        "methodology": {"validation": "rolling out-of-sample forward returns"},
    }
    with patch("services.tw_factor_service.validate_factor_ranking", new_callable=AsyncMock) as mock:
        mock.return_value = payload
        response = await client.get(
            "/api/tw/factor-validation?start_date=2024-01-01&end_date=2025-01-01"
            "&profile=balanced&top_n=20&holding_sessions=21&transaction_cost_bps=25",
            headers=h,
        )

    assert response.status_code == 200
    assert response.json()["periods"][0]["net_return_pct"] == pytest.approx(2.75)
    mock.assert_awaited_once_with(
        start_date=date(2024, 1, 1), end_date=date(2025, 1, 1), profile="balanced",
        top_n=20, holding_sessions=21, transaction_cost_bps=25,
        sector_neutral=True,
        portfolio_notional_twd=10_000_000,
        max_participation_rate=.05, impact_coefficient_bps=10,
        benchmark="taiex_total_return",
        weight_mode="walk_forward",
    )


@pytest.mark.asyncio
async def test_factor_research_registry_persists_gates_and_promotes(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor_registry@example.com")
    validation = {
        "market": "TW", "profile": "balanced",
        "methodology_version": "tw-explainable-multifactor-v8",
        "benchmark_requested": "taiex_total_return",
        "benchmark_used": "taiex_total_return",
        "summary": {
            "period_count": 30, "average_excess_return_pct": .6,
            "positive_excess_rate_pct": 60, "max_drawdown_pct": -8,
            "average_fill_pct": 92,
        },
        "factor_diagnostics": {"composite": {
            "average_rank_ic": .08, "significant_after_holm_5pct": True,
        }},
        "weight_stability": {
            "adaptive_period_count": 18, "maximum_weight_turnover_pct": 4,
            "factor_ranges": {
                "value": {"latest": .24}, "quality": {"latest": .17},
                "momentum": {"latest": .19}, "low_volatility": {"latest": .15},
                "income": {"latest": .10}, "liquidity": {"latest": .15},
            },
        },
        "quality": {"status": "good", "flags": []},
    }
    body = {
        "name": "governed challenger", "start_date": "2021-01-01",
        "end_date": "2025-01-01", "auto_promote": False,
    }
    with patch(
        "services.tw_factor_service.validate_factor_ranking",
        new_callable=AsyncMock, return_value=validation,
    ) as mock:
        response = await client.post("/api/tw/factor-research-runs", json=body, headers=h)
    assert response.status_code == 200
    created = response.json()
    assert created["run"]["gate_result"]["eligible"] is True
    assert created["model"]["status"] == "candidate"
    assert sum(created["model"]["weights"].values()) == pytest.approx(1)
    mock.assert_awaited_once()

    listed = await client.get("/api/tw/factor-research-runs", headers=h)
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    models = await client.get("/api/tw/factor-models?profile=balanced", headers=h)
    assert models.status_code == 200
    assert len(models.json()) == 1

    promoted = await client.post(
        f"/api/tw/factor-models/{created['model']['id']}/promote", headers=h,
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "champion"

    ranking = {
        "market": "TW", "as_of": "2025-01-01", "profile": "balanced",
        "methodology_version": "tw-explainable-multifactor-v8",
        "weights": created["model"]["weights"], "candidates": [],
        "quality": _factor_quality(), "methodology": {}, "sector_neutral": True,
        "weight_source": "champion", "model_id": created["model"]["id"],
    }
    with patch(
        "services.tw_factor_service.get_factor_ranking",
        new_callable=AsyncMock, return_value=ranking,
    ) as rank_mock:
        ranked = await client.get("/api/tw/factor-ranking?profile=balanced", headers=h)
    assert ranked.status_code == 200
    assert ranked.json()["weight_source"] == "champion"
    call = rank_mock.await_args.kwargs
    assert call["model_id"] == created["model"]["id"]
    assert call["weights_override"]["quality"] == pytest.approx(.17)


@pytest.mark.asyncio
async def test_factor_validation_caps_window(client: AsyncClient):
    h = await _auth_headers(client, "tw_factor_window@example.com")
    response = await client.get(
        "/api/tw/factor-validation?start_date=2019-01-01&end_date=2025-01-01", headers=h,
    )
    assert response.status_code == 400
