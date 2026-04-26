"""Pure-unit tests for the S&P 500 ticker loader fallback behavior."""
from unittest.mock import AsyncMock, patch

import pytest

import data.us.sp500_universe as sp500


@pytest.fixture(autouse=True)
def _reset_cache():
    sp500._cache = []
    yield
    sp500._cache = []


@pytest.mark.asyncio
async def test_returns_wikipedia_result_when_fetch_succeeds():
    fetched = ["AAPL", "MSFT", "GOOGL"]
    with patch.object(sp500, "_fetch_from_wikipedia", new_callable=AsyncMock, return_value=fetched):
        result = await sp500.get_sp500_tickers()
    assert result == fetched


@pytest.mark.asyncio
async def test_falls_back_to_static_list_when_fetch_raises():
    with patch.object(
        sp500, "_fetch_from_wikipedia",
        new_callable=AsyncMock, side_effect=RuntimeError("network down"),
    ):
        result = await sp500.get_sp500_tickers()
    assert result == sp500._FALLBACK_TICKERS
    # Cache populated so subsequent calls don't re-attempt the fetch.
    assert sp500._cache == sp500._FALLBACK_TICKERS


@pytest.mark.asyncio
async def test_falls_back_when_fetch_returns_empty():
    """Wikipedia HTML structure changes → regex finds nothing → fallback."""
    with patch.object(sp500, "_fetch_from_wikipedia", new_callable=AsyncMock, return_value=[]):
        result = await sp500.get_sp500_tickers()
    assert result == sp500._FALLBACK_TICKERS


@pytest.mark.asyncio
async def test_force_refresh_replaces_cache_with_fresh_data():
    sp500._cache = ["OLD"]
    with patch.object(sp500, "_fetch_from_wikipedia", new_callable=AsyncMock, return_value=["AAPL", "MSFT"]):
        result = await sp500.get_sp500_tickers(force_refresh=True)
    assert result == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_force_refresh_failure_preserves_existing_cache():
    """A failed force-refresh must NOT clobber a previously good cache."""
    sp500._cache = ["AAPL", "MSFT", "GOOGL"]
    with patch.object(
        sp500, "_fetch_from_wikipedia",
        new_callable=AsyncMock, side_effect=RuntimeError("rate-limited"),
    ):
        result = await sp500.get_sp500_tickers(force_refresh=True)
    assert result == ["AAPL", "MSFT", "GOOGL"]


def test_fallback_list_includes_top_tickers_for_search():
    """A user searching for any of these expects results immediately."""
    for sym in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA", "META",
                "SPY", "QQQ", "BRK-B"):
        assert sym in sp500._FALLBACK_TICKERS, f"missing {sym} from fallback list"


def test_fallback_universe_has_company_names():
    """get_fallback_universe pairs symbols with display names so the screener
    can render rows even when yfinance is unreachable."""
    pairs = sp500.get_fallback_universe()
    by_symbol = dict(pairs)
    assert by_symbol["AAPL"] == "Apple Inc."
    assert by_symbol["NVDA"] == "NVIDIA Corporation"
    assert by_symbol["SPY"].startswith("SPDR S&P 500")
    # No empty names
    for sym, name in pairs:
        assert sym, "empty symbol in fallback universe"
        assert name, f"empty name for {sym}"


def test_fallback_universe_has_no_duplicate_symbols():
    seen: set[str] = set()
    for sym, _ in sp500.get_fallback_universe():
        assert sym not in seen, f"duplicate symbol {sym!r} in fallback universe"
        seen.add(sym)


def test_fallback_universe_covers_major_sectors():
    """At least one ticker from each major sector so any preset returns hits."""
    by_symbol = dict(sp500.get_fallback_universe())
    # Tech / semis / cloud
    assert any(s in by_symbol for s in ("AAPL", "MSFT", "NVDA", "AMD"))
    # Financials
    assert any(s in by_symbol for s in ("JPM", "BAC", "GS"))
    # Healthcare
    assert any(s in by_symbol for s in ("UNH", "JNJ", "LLY"))
    # Energy
    assert any(s in by_symbol for s in ("XOM", "CVX", "COP"))
    # REITs
    assert any(s in by_symbol for s in ("PLD", "AMT", "O"))
    # Materials
    assert any(s in by_symbol for s in ("LIN", "FCX", "NEM"))
    # Sector ETFs
    assert any(s in by_symbol for s in ("XLK", "XLF", "XLE"))


def test_fallback_universe_size_meets_minimum():
    """We promise users a meaningful list — at least 200 names."""
    assert len(sp500.get_fallback_universe()) >= 200
