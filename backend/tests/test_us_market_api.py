"""
Integration tests for the US market API endpoints.
All external data calls (Polygon, yfinance) are mocked so CI runs offline.
"""
import time
import pytest
from httpx import AsyncClient
from unittest.mock import AsyncMock, patch


# ── helpers ───────────────────────────────────────────────────────

async def _auth_headers(client: AsyncClient, email: str = "us_user@example.com") -> dict:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass1234!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass1234!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mock_quote(symbol: str = "AAPL") -> dict:
    return {
        "symbol": symbol, "market": "US", "name": "Apple Inc.",
        "price": 182.50, "change": 1.25, "change_pct": 0.69,
        "volume": 55_000_000, "open": 181.0, "high": 183.5, "low": 180.0,
        "prev_close": 181.25, "market_cap": 2.85e12, "currency": "USD",
        "ts": int(time.time() * 1000), "is_market_open": True,
    }


def _mock_bars(n: int = 5) -> list[dict]:
    return [
        {"time": f"2024-01-{i + 1:02d}", "open": 180.0, "high": 185.0,
         "low": 179.0, "close": 182.0 + i, "volume": 50_000_000}
        for i in range(n)
    ]


def _mock_fundamentals(symbol: str = "AAPL") -> dict:
    return {
        "symbol": symbol, "market": "US", "name": "Apple Inc.",
        "sector": "Technology", "industry": "Consumer Electronics",
        "market_cap": 2.85e12, "pe_ratio": 28.5, "pb_ratio": 45.2,
        "eps": 6.42, "dividend_yield": 0.0055, "beta": 1.24,
        "52w_high": 199.6, "52w_low": 124.2, "description": "Apple Inc. designs ...",
        "fetched_at": "2024-01-15T12:00:00Z",
    }


def _mock_screener_items() -> list[dict]:
    return [
        {"symbol": "AAPL", "market": "US", "name": "Apple Inc.",
         "price": 182.5, "change_pct": 0.69, "volume": 55_000_000,
         "market_cap": 2.85e12, "pe_ratio": 28.5, "sector": "Technology"},
        {"symbol": "MSFT", "market": "US", "name": "Microsoft Corp.",
         "price": 412.0, "change_pct": 0.25, "volume": 22_000_000,
         "market_cap": 3.1e12, "pe_ratio": 36.0, "sector": "Technology"},
    ]


# ── auth enforcement ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quote_requires_auth(client: AsyncClient):
    r = await client.get("/api/us/quote/AAPL")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_history_requires_auth(client: AsyncClient):
    r = await client.get("/api/us/history/AAPL")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_fundamentals_requires_auth(client: AsyncClient):
    r = await client.get("/api/us/fundamentals/AAPL")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_screener_requires_auth(client: AsyncClient):
    r = await client.get("/api/us/screener")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_news_requires_auth(client: AsyncClient):
    r = await client.get("/api/us/news/AAPL")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_search_requires_auth(client: AsyncClient):
    r = await client.get("/api/us/search?q=AAPL")
    assert r.status_code == 401


# ── quote ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quote_shape(client: AsyncClient):
    h = await _auth_headers(client, "quote_shape@example.com")
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_quote("AAPL")
        r = await client.get("/api/us/quote/AAPL", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "AAPL"
    assert data["market"] == "US"
    assert data["price"] == pytest.approx(182.50)
    assert "change_pct" in data
    assert "volume" in data
    assert "currency" in data
    assert "is_market_open" in data


@pytest.mark.asyncio
async def test_quote_ticker_uppercased(client: AsyncClient):
    """Router should normalise lowercase ticker to uppercase."""
    h = await _auth_headers(client, "quote_upper@example.com")
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_quote("TSLA")
        await client.get("/api/us/quote/tsla", headers=h)

    mock.assert_awaited_once_with("TSLA")


# ── history ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_returns_bars(client: AsyncClient):
    h = await _auth_headers(client, "hist_bars@example.com")
    bars = _mock_bars(10)
    with patch("services.us_market_service.get_history", new_callable=AsyncMock) as mock:
        mock.return_value = bars
        r = await client.get("/api/us/history/MSFT?period=1mo&interval=1d", headers=h)

    assert r.status_code == 200
    result = r.json()
    assert len(result) == 10
    bar = result[0]
    for field in ("time", "open", "high", "low", "close", "volume"):
        assert field in bar


@pytest.mark.asyncio
async def test_history_default_params_passed(client: AsyncClient):
    """Default period=1y and interval=1d should be forwarded to the service."""
    h = await _auth_headers(client, "hist_defaults@example.com")
    with patch("services.us_market_service.get_history", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_bars(3)
        await client.get("/api/us/history/AAPL", headers=h)

    mock.assert_awaited_once_with("AAPL", period="1y", interval="1d")


# ── fundamentals ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fundamentals_shape(client: AsyncClient):
    h = await _auth_headers(client, "fund_shape@example.com")
    with patch("services.us_market_service.get_fundamentals", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_fundamentals("GOOGL")
        r = await client.get("/api/us/fundamentals/GOOGL", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "GOOGL"
    assert data["sector"] == "Technology"
    assert "pe_ratio" in data
    assert "market_cap" in data
    assert "fetched_at" in data


@pytest.mark.asyncio
async def test_fundamentals_missing_fields_nullable(client: AsyncClient):
    """Fundamentals with minimal data should still pass response model validation."""
    h = await _auth_headers(client, "fund_null@example.com")
    with patch("services.us_market_service.get_fundamentals", new_callable=AsyncMock) as mock:
        mock.return_value = {
            "symbol": "XYZ", "market": "US", "fetched_at": "2024-01-01T00:00:00Z"
        }
        r = await client.get("/api/us/fundamentals/XYZ", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["pe_ratio"] is None
    assert data["sector"] is None


# ── screener ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_screener_returns_list(client: AsyncClient):
    h = await _auth_headers(client, "scr_list@example.com")
    with patch("services.us_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = _mock_screener_items()
        r = await client.get("/api/us/screener", headers=h)

    assert r.status_code == 200
    items = r.json()
    assert len(items) == 2
    assert items[0]["symbol"] == "AAPL"
    assert "price" in items[0]


@pytest.mark.asyncio
async def test_screener_passes_filters(client: AsyncClient):
    """Query-string filters should be forwarded to the service."""
    h = await _auth_headers(client, "scr_filter@example.com")
    with patch("services.us_market_service.get_screener", new_callable=AsyncMock) as mock:
        mock.return_value = []
        await client.get(
            "/api/us/screener?min_market_cap=1000000000&max_pe=30&sector=Technology",
            headers=h,
        )

    _, kwargs = mock.call_args
    assert kwargs["min_market_cap"] == pytest.approx(1e9)
    assert kwargs["max_pe"] == pytest.approx(30.0)
    assert kwargs["sector"] == "Technology"


# ── news ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_news_returns_list(client: AsyncClient):
    h = await _auth_headers(client, "news_list@example.com")
    items = [
        {"title": "Apple hits record", "url": "https://example.com/1",
         "published_at": "2024-01-15T10:00:00Z", "source": "Reuters"},
        {"title": "iPhone sales up", "url": "https://example.com/2",
         "published_at": "2024-01-14T09:00:00Z", "source": "Bloomberg"},
    ]
    with patch("services.us_market_service.get_news", new_callable=AsyncMock) as mock:
        mock.return_value = items
        r = await client.get("/api/us/news/AAPL?limit=5", headers=h)

    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.asyncio
async def test_news_empty_list(client: AsyncClient):
    h = await _auth_headers(client, "news_empty@example.com")
    with patch("services.us_market_service.get_news", new_callable=AsyncMock) as mock:
        mock.return_value = []
        r = await client.get("/api/us/news/FAKE", headers=h)
    assert r.status_code == 200
    assert r.json() == []


# ── search ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_matches(client: AsyncClient):
    h = await _auth_headers(client, "search_match@example.com")
    with patch("services.us_market_service._get_sp500_tickers", new_callable=AsyncMock) as mock:
        mock.return_value = ["AAPL", "AAPLX", "MSFT", "GOOG"]
        r = await client.get("/api/us/search?q=AAPL", headers=h)

    assert r.status_code == 200
    results = r.json()
    symbols = [item["symbol"] for item in results]
    assert "AAPL" in symbols
    assert "AAPLX" in symbols
    assert "MSFT" not in symbols


@pytest.mark.asyncio
async def test_search_case_insensitive(client: AsyncClient):
    h = await _auth_headers(client, "search_case@example.com")
    with patch("services.us_market_service._get_sp500_tickers", new_callable=AsyncMock) as mock:
        mock.return_value = ["AAPL", "MSFT"]
        r = await client.get("/api/us/search?q=aapl", headers=h)

    assert r.status_code == 200
    assert any(item["symbol"] == "AAPL" for item in r.json())


@pytest.mark.asyncio
async def test_search_empty_query_rejected(client: AsyncClient):
    """q is required and has min_length=1."""
    h = await _auth_headers(client, "search_empty@example.com")
    r = await client.get("/api/us/search?q=", headers=h)
    assert r.status_code == 422


# ── earnings ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_earnings_shape(client: AsyncClient):
    h = await _auth_headers(client, "earn_shape@example.com")
    with patch("services.us_market_service.get_earnings", new_callable=AsyncMock) as mock:
        mock.return_value = {
            "earnings_date": "2024-02-01",
            "eps_estimate": 2.10,
            "revenue_estimate": 1.25e11,
        }
        r = await client.get("/api/us/earnings/AAPL", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert "earnings_date" in data
    assert "eps_estimate" in data
    assert "revenue_estimate" in data


@pytest.mark.asyncio
async def test_earnings_null_when_unavailable(client: AsyncClient):
    h = await _auth_headers(client, "earn_null@example.com")
    with patch("services.us_market_service.get_earnings", new_callable=AsyncMock) as mock:
        mock.return_value = {
            "earnings_date": None,
            "eps_estimate": None,
            "revenue_estimate": None,
        }
        r = await client.get("/api/us/earnings/FAKE", headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["earnings_date"] is None
