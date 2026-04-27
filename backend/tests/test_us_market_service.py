"""Pure unit tests for the US market service options-chain fallback.

The service used to return [] when POLYGON_API_KEY was unset; now it
falls through to yfinance.get_options() so a free-tier deployment still
serves option chains.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from services import us_market_service as svc


def _yf_chain_row(strike: float = 200.0, ctype: str = "call") -> dict:
    return {
        "ticker": f"AAPL250620C{int(strike * 1000):08d}",
        "underlying_ticker": "AAPL",
        "contract_type": ctype,
        "expiration_date": "2025-06-20",
        "strike_price": strike,
        "last_price": 5.25,
        "bid": 5.20,
        "ask": 5.30,
        "volume": 1000,
        "open_interest": 5000,
        "implied_volatility": 0.28,
        "in_the_money": False,
    }


@pytest.mark.asyncio
async def test_get_options_cache_hit_skips_providers():
    cached = json.dumps([_yf_chain_row()])
    with patch.object(svc, "cache_get", AsyncMock(return_value=cached)), \
         patch.object(svc.polygon, "get_options_chain", AsyncMock()) as mp, \
         patch.object(svc.yfinance, "get_options", AsyncMock()) as my:
        result = await svc.get_options("AAPL")
    mp.assert_not_awaited()
    my.assert_not_awaited()
    assert len(result) == 1


@pytest.mark.asyncio
async def test_get_options_yfinance_fallback_when_no_polygon_key():
    """No Polygon key set ⇒ go straight to yfinance."""
    fallback_rows = [_yf_chain_row(200.0), _yf_chain_row(210.0, "put")]
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.polygon, "get_options_chain", AsyncMock()) as mp, \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=fallback_rows)) as my:
        result = await svc.get_options("AAPL")

    mp.assert_not_awaited()
    my.assert_awaited_once_with("AAPL", None)
    mock_set.assert_awaited_once()
    assert result == fallback_rows


@pytest.mark.asyncio
async def test_get_options_yfinance_fallback_when_polygon_raises():
    """Polygon configured but raised ⇒ fall through to yfinance."""
    fallback_rows = [_yf_chain_row()]
    with patch.object(svc.settings, "POLYGON_API_KEY", "test-key"), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.polygon, "get_options_chain", AsyncMock(side_effect=RuntimeError("rate limited"))) as mp, \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=fallback_rows)) as my:
        result = await svc.get_options("AAPL")

    mp.assert_awaited_once()
    my.assert_awaited_once_with("AAPL", None)
    assert result == fallback_rows


@pytest.mark.asyncio
async def test_get_options_empty_result_not_cached():
    """Both providers returned [] — caching that would lock TTL_OPTIONS of empty."""
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=[])):
        result = await svc.get_options("FAKE")

    mock_set.assert_not_awaited()
    assert result == []


@pytest.mark.asyncio
async def test_get_options_passes_expiration_date_through():
    fallback_rows = [_yf_chain_row()]
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=fallback_rows)) as my:
        await svc.get_options("AAPL", "2025-06-20")
    my.assert_awaited_once_with("AAPL", "2025-06-20")


# ── get_screener — Stooq batch cap (issue: /api/us/screener timeout) ──
#
# The default sync request path must NOT hand the entire 100-symbol
# universe to Stooq's 5-syms/sec endpoint. The background warm task
# (full_stooq_batch=True) is the only caller that's allowed to wait
# for the full walk.

def _fake_universe(n: int) -> list[tuple[str, str]]:
    return [(f"SYM{i:03d}", f"Company {i}") for i in range(n)]


@pytest.mark.asyncio
async def test_get_screener_caps_stooq_batch_in_sync_path():
    """Sync request: Stooq must only see the first STOOQ_SYNC_BATCH_LIMIT
    symbols even when the universe is much larger."""
    universe = _fake_universe(100)
    captured: dict[str, list[str]] = {}

    async def fake_stooq_batch(syms: list[str]):
        captured["syms"] = list(syms)
        return {s: {"price": 10.0, "change_pct": 0.5, "volume": 1000} for s in syms}

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_batch_quotes", new=fake_stooq_batch):
        rows = await svc.get_screener(limit=100)

    assert len(captured["syms"]) == svc.STOOQ_SYNC_BATCH_LIMIT
    # All 100 universe rows are still returned (uncapped symbols just
    # have price=0 so the page renders something instead of 無資料).
    assert len(rows) == 100
    priced = [r for r in rows if r["price"] > 0]
    assert len(priced) == svc.STOOQ_SYNC_BATCH_LIMIT


@pytest.mark.asyncio
async def test_get_screener_full_stooq_batch_for_warm_task():
    """Background warm task passes full_stooq_batch=True ⇒ Stooq receives
    the entire universe even though the sync path would have capped it."""
    universe = _fake_universe(100)
    captured: dict[str, list[str]] = {}

    async def fake_stooq_batch(syms: list[str]):
        captured["syms"] = list(syms)
        return {s: {"price": 10.0, "change_pct": 0.5, "volume": 1000} for s in syms}

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_batch_quotes", new=fake_stooq_batch):
        await svc.get_screener(limit=100, full_stooq_batch=True)

    assert len(captured["syms"]) == 100  # uncapped


@pytest.mark.asyncio
async def test_get_screener_caches_zero_price_rows_briefly():
    """When every quote provider is blocked we still cache the universe
    shell (price=0) for a short TTL so the next user sees something
    immediately rather than re-running the whole waterfall."""
    universe = _fake_universe(50)
    set_calls: list[tuple] = []

    async def capture_set(*args, **kwargs):
        set_calls.append(args)

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", new=capture_set), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_batch_quotes", AsyncMock(return_value={})):
        rows = await svc.get_screener(limit=50)

    assert len(rows) == 50
    assert all(r["price"] == 0.0 for r in rows)
    # Cached with the short fallback TTL (60s), not the full 10-min TTL.
    assert len(set_calls) == 1
    assert set_calls[0][2] == 60
