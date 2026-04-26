"""
Pure-unit tests for the yfinance-backed US screener path. Mocks the
yfinance connector + Redis + sp500 universe so the test runs offline
without aiosqlite/passlib.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.us_market_service as svc


def _info(
    symbol: str,
    *,
    cap: float = 0,
    pe: float | None = None,
    pb: float | None = None,
    dy: float | None = None,    # decimal fraction, eg 0.025 = 2.5%
    vol: int = 0,
    sector: str = "",
    name: str | None = None,
    price: float = 100.0,
) -> dict:
    out = {
        "marketCap": cap,
        "trailingPE": pe,
        "priceToBook": pb,
        "dividendYield": dy,
        "volume": vol,
        "sector": sector,
        "longName": name or symbol,
        "currentPrice": price,
        "regularMarketChangePercent": 0,
    }
    return out


@pytest.mark.asyncio
async def test_screener_yfinance_filters_by_pe_pb_yield():
    """All three fundamental filters reject candidates correctly."""
    tickers = ["A", "B", "C"]
    info_map = {
        # passes everything
        "A": _info("A", cap=20e9, pe=12, pb=1.2, dy=0.04, vol=2_000_000, sector="Tech"),
        # PE too high
        "B": _info("B", cap=20e9, pe=40, pb=1.2, dy=0.04, vol=2_000_000, sector="Tech"),
        # yield too low
        "C": _info("C", cap=20e9, pe=12, pb=1.2, dy=0.01, vol=2_000_000, sector="Tech"),
    }

    async def _get_info(t):
        return info_map[t]

    with patch.object(svc.yfinance, "get_info", new=_get_info):
        out = await svc._screener_yfinance(
            tickers,
            max_pe=20,
            min_pb=1.0,
            max_pb=3.0,
            min_dividend_yield=3.0,
            limit=10,
        )

    symbols = [r["symbol"] for r in out]
    assert symbols == ["A"]
    # Yield is reported as percent (0.04 → 4.0)
    assert out[0]["dividend_yield"] == pytest.approx(4.0)
    assert out[0]["pb_ratio"] == pytest.approx(1.2)


@pytest.mark.asyncio
async def test_screener_yfinance_skips_when_field_missing():
    """Missing PE/PB/yield should be treated as failing the filter, not passing."""
    tickers = ["A", "B"]
    info_map = {
        "A": _info("A", cap=20e9, pe=10, pb=None, dy=0.04),
        "B": _info("B", cap=20e9, pe=10, pb=1.2, dy=0.04),
    }

    async def _get_info(t):
        return info_map[t]

    with patch.object(svc.yfinance, "get_info", new=_get_info):
        out = await svc._screener_yfinance(tickers, max_pb=5.0, limit=10)

    # A has no PB → rejected; B passes
    assert [r["symbol"] for r in out] == ["B"]


@pytest.mark.asyncio
async def test_screener_yfinance_yield_in_percent():
    """The result's dividend_yield is in percent (3.5), not fraction (0.035)."""
    info_map = {"X": _info("X", cap=10e9, dy=0.0312, sector="Utilities")}
    with patch.object(svc.yfinance, "get_info", new=AsyncMock(return_value=info_map["X"])):
        out = await svc._screener_yfinance(["X"], limit=1)

    assert out[0]["dividend_yield"] == pytest.approx(3.12, abs=0.01)


@pytest.mark.asyncio
async def test_get_screener_forces_yfinance_when_fundamental_filter_set():
    """Polygon's bulk snapshot can't filter PE/PB/yield, so the service
    falls back to yfinance whenever a fundamental filter is in play."""
    polygon_called = AsyncMock()

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_use_polygon", return_value=True), \
         patch.object(svc.polygon, "get_snapshot_all", new=polygon_called), \
         patch.object(svc, "_get_sp500_tickers", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc, "_screener_yfinance", new_callable=AsyncMock, return_value=[]) as yf_mock:
        await svc.get_screener(min_dividend_yield=3.0)

    polygon_called.assert_not_called()
    yf_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_screener_uses_polygon_when_no_fundamental_filter():
    """Plain market_cap/volume filters still go through Polygon if available."""
    snaps = [
        {"ticker": "AAPL", "day": {"c": 175, "v": 50_000_000},
         "todaysChangePerc": 0.5, "details": {"name": "Apple Inc."}},
    ]

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_use_polygon", return_value=True), \
         patch.object(svc.polygon, "get_snapshot_all", new_callable=AsyncMock, return_value=snaps), \
         patch.object(svc, "_screener_yfinance", new_callable=AsyncMock) as yf_mock:
        result = await svc.get_screener(min_volume=1_000_000)

    yf_mock.assert_not_called()
    assert result[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_get_screener_falls_through_to_yfinance_when_polygon_returns_empty():
    """Transient Polygon failure → fall through so users still see results."""
    yf_result = [{"symbol": "AAPL", "market": "US", "name": "Apple Inc.",
                  "price": 175.0, "change_pct": 0, "volume": 1_000_000}]

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_use_polygon", return_value=True), \
         patch.object(svc.polygon, "get_snapshot_all", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc, "_get_sp500_tickers", new_callable=AsyncMock, return_value=["AAPL"]), \
         patch.object(svc, "_screener_yfinance", new_callable=AsyncMock, return_value=yf_result) as yf_mock:
        result = await svc.get_screener()

    yf_mock.assert_awaited_once()
    assert result == yf_result


@pytest.mark.asyncio
async def test_get_screener_falls_through_when_polygon_raises():
    """Polygon API raising shouldn't bubble — fall through to yfinance."""
    yf_result = [{"symbol": "AAPL", "market": "US", "name": "Apple Inc.",
                  "price": 175.0, "change_pct": 0, "volume": 1_000_000}]

    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch.object(svc, "_use_polygon", return_value=True), \
         patch.object(svc.polygon, "get_snapshot_all", new_callable=AsyncMock,
                      side_effect=RuntimeError("polygon down")), \
         patch.object(svc, "_get_sp500_tickers", new_callable=AsyncMock, return_value=["AAPL"]), \
         patch.object(svc, "_screener_yfinance", new_callable=AsyncMock, return_value=yf_result):
        result = await svc.get_screener()

    assert result == yf_result


@pytest.mark.asyncio
async def test_get_screener_does_not_cache_empty_result():
    """Empty result should not lock in a cache miss for TTL_SCREENER seconds."""
    cache_set = AsyncMock()
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new=cache_set), \
         patch.object(svc, "_use_polygon", return_value=False), \
         patch.object(svc, "_get_sp500_tickers", new_callable=AsyncMock, return_value=[]), \
         patch.object(svc, "_screener_yfinance", new_callable=AsyncMock, return_value=[]):
        result = await svc.get_screener()

    assert result == []
    cache_set.assert_not_called()
