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


@pytest.mark.asyncio
async def test_get_options_tags_data_source_yfinance_when_no_polygon():
    """Free-tier deployments (no Polygon key) get the yfinance chain. Each
    row should carry data_source="yfinance" so the OptionsPanel can show
    the bid/ask-spreads-unavailable hint."""
    rows = [_yf_chain_row(200.0), _yf_chain_row(210.0, "put")]
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_options", AsyncMock(return_value=rows)):
        result = await svc.get_options("AAPL")
    assert all(r["data_source"] == "yfinance" for r in result)


@pytest.mark.asyncio
async def test_get_options_tags_data_source_polygon_when_polygon_serves():
    """Paid-tier path: Polygon returned the chain ⇒ rows are tagged
    "polygon" so the OptionsPanel does NOT render the free-tier hint."""
    polygon_rows = [{"ticker": "AAPL250620C00200000", "underlying_ticker": "AAPL",
                     "contract_type": "call", "expiration_date": "2025-06-20",
                     "strike_price": 200.0}]
    with patch.object(svc.settings, "POLYGON_API_KEY", "test-key"), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.polygon, "get_options_chain",
                      AsyncMock(return_value=polygon_rows)), \
         patch.object(svc.yfinance, "get_options", AsyncMock()) as my:
        result = await svc.get_options("AAPL")
    my.assert_not_awaited()
    assert all(r["data_source"] == "polygon" for r in result)


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
async def test_get_screener_skips_screener_yfinance_when_no_polygon_and_no_filter():
    """Without Polygon AND without fundamental filters we MUST skip
    `_screener_yfinance` (slow per-symbol .info calls that Yahoo
    IP-blocks for 30 s+) and go straight to the universe path.
    Otherwise the request hangs past the reverse-proxy timeout."""
    universe = _fake_universe(50)
    yfinance_screener_called = False

    async def fake_screener_yfinance(*_a, **_kw):
        nonlocal yfinance_screener_called
        yfinance_screener_called = True
        return []

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", new=fake_screener_yfinance), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_batch_quotes", AsyncMock(return_value={})):
        rows = await svc.get_screener(limit=50)

    assert yfinance_screener_called is False, \
        "_screener_yfinance must NOT be called when no Polygon and no filter"
    assert len(rows) == 50  # universe shells returned immediately


@pytest.mark.asyncio
async def test_get_screener_still_uses_yfinance_when_filter_active():
    """When the caller asks for fundamental filters (PE / PB / sector /
    market_cap / volume) we DO need _screener_yfinance — those fields
    only come from yfinance.get_info."""
    yfinance_screener_called = False

    async def fake_screener_yfinance(*_a, **_kw):
        nonlocal yfinance_screener_called
        yfinance_screener_called = True
        return [{"symbol": "AAPL", "market": "US", "name": "Apple",
                 "price": 100, "change_pct": 1, "volume": 1000,
                 "market_cap": 1e12, "pe_ratio": 25, "pb_ratio": 4,
                 "dividend_yield": 0.5, "sector": "Tech"}]

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", new=fake_screener_yfinance), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=["AAPL"])):
        rows = await svc.get_screener(min_pe=10)

    assert yfinance_screener_called is True
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_get_screener_min_volume_falls_through_to_batch():
    """Regression: when only `min_volume` is set, `_screener_yfinance` is
    still called (volume requires .info) but if it returns nothing we MUST
    fall through to the batch_quotes path and apply min_volume post-hoc.
    Previously the third fallback short-circuited on `not min_volume`,
    leaving the user with a blank page."""
    universe = _fake_universe(10)
    quotes = {
        # half meet the min_volume threshold
        f"SYM{i:03d}": {"price": 10.0, "change_pct": 0.0,
                        "volume": 5_000_000 if i < 5 else 100_000}
        for i in range(10)
    }

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value=quotes)), \
         patch.object(svc.stooq, "get_batch_quotes", AsyncMock(return_value={})):
        rows = await svc.get_screener(limit=10, min_volume=1_000_000)

    assert len(rows) == 5
    assert all((r["volume"] or 0) >= 1_000_000 for r in rows)


@pytest.mark.asyncio
async def test_get_quote_records_data_source():
    """The normalized quote payload now carries `data_source` so the
    frontend can distinguish `polygon`/`yfinance`/`stooq`/`unavailable`."""
    yf_quote = {
        "symbol": "AAPL", "price": 195.0, "change": 1.0, "change_pct": 0.5,
        "volume": 12345, "open": 194, "high": 196, "low": 193,
        "prev_close": 194.0, "market_cap": 3_000_000_000_000,
        "ts": 1700000000000,
    }
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_quote", AsyncMock(return_value=yf_quote)):
        result = await svc.get_quote("AAPL")
    assert result["data_source"] == "yfinance"
    assert result["price"] == 195.0


@pytest.mark.asyncio
async def test_get_quote_data_source_unavailable_when_all_fail():
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.yfinance, "get_quote", AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch.object(svc.stooq, "get_quote", AsyncMock(side_effect=RuntimeError("blocked"))), \
         patch.object(svc.finnhub, "get_quote", AsyncMock(return_value={})):
        result = await svc.get_quote("AAPL")
    assert result["data_source"] == "unavailable"
    assert result["price"] == 0
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_quote_falls_through_to_finnhub_when_others_blank():
    """Polygon disabled, yfinance + Stooq both empty → Finnhub is the
    last waterfall step. Tag must say `finnhub`."""
    finnhub_quote = {
        "symbol": "AAPL", "price": 200.0, "change": 1.0, "change_pct": 0.5,
        "volume": 0, "open": 199, "high": 201, "low": 198,
        "prev_close": 199.0, "market_cap": None,
    }
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_quote", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_quote", AsyncMock(return_value={})), \
         patch.object(svc.finnhub, "get_quote", AsyncMock(return_value=finnhub_quote)):
        result = await svc.get_quote("AAPL")
    assert result["data_source"] == "finnhub"
    assert result["price"] == 200.0


@pytest.mark.asyncio
async def test_get_screener_falls_through_to_finnhub_batch():
    """yfinance batch + Stooq batch both empty → screener falls through
    to finnhub.get_batch_quotes and tags resulting rows `finnhub`."""
    universe = _fake_universe(3)
    finnhub_quotes = {
        "SYM000": {"price": 50.0, "change_pct": 1.0, "volume": 100},
        "SYM002": {"price": 70.0, "change_pct": 2.0, "volume": 300},
    }

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.finnhub, "get_batch_quotes", AsyncMock(return_value=finnhub_quotes)):
        rows = await svc.get_screener(limit=3)

    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["SYM000"]["data_source"] == "finnhub"
    assert by_sym["SYM002"]["data_source"] == "finnhub"
    assert by_sym["SYM001"]["data_source"] == "unavailable"


@pytest.mark.asyncio
async def test_get_screener_marks_data_source_yfinance_when_batch_serves():
    """Batch fallback rows that yfinance.get_batch_quotes actually filled
    should be tagged `yfinance`; symbols that even Stooq couldn't price
    are tagged `unavailable` so the UI can flag them."""
    universe = _fake_universe(4)
    quotes = {  # yfinance fills 2 of 4
        "SYM000": {"price": 1.0, "change_pct": 0.0, "volume": 100},
        "SYM002": {"price": 3.0, "change_pct": 0.0, "volume": 300},
    }

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value=quotes)), \
         patch.object(svc.stooq, "get_batch_quotes", AsyncMock(return_value={})):
        rows = await svc.get_screener(limit=4)

    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["SYM000"]["data_source"] == "yfinance"
    assert by_sym["SYM002"]["data_source"] == "yfinance"
    assert by_sym["SYM001"]["data_source"] == "unavailable"
    assert by_sym["SYM003"]["data_source"] == "unavailable"


@pytest.mark.asyncio
async def test_get_screener_marks_data_source_stooq_when_yfinance_blank():
    """yfinance.get_batch_quotes returned nothing → we fell to Stooq.
    Rows Stooq priced should now carry `stooq` so the UI badge fires."""
    universe = _fake_universe(2)
    stooq_quotes = {
        "SYM000": {"price": 5.0, "change_pct": 0.1, "volume": 999},
        "SYM001": {"price": 6.0, "change_pct": -0.1, "volume": 888},
    }

    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc, "_screener_yfinance", AsyncMock(return_value=[])), \
         patch.object(svc, "_get_sp500_tickers", AsyncMock(return_value=[s for s, _ in universe])), \
         patch("data.us.sp500_universe.get_fallback_universe", lambda: universe), \
         patch.object(svc.yfinance, "get_batch_quotes", AsyncMock(return_value={})), \
         patch.object(svc.stooq, "get_batch_quotes", AsyncMock(return_value=stooq_quotes)):
        rows = await svc.get_screener(limit=2)

    assert all(r["data_source"] == "stooq" for r in rows)


@pytest.mark.asyncio
async def test_get_fundamentals_marks_data_source_yfinance():
    """No Polygon key ⇒ yfinance.get_info served the payload ⇒ row carries
    data_source="yfinance"."""
    info = {"longName": "Apple", "sector": "Tech", "marketCap": 3e12,
            "trailingPE": 30, "priceToBook": 40}
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_info", AsyncMock(return_value=info)):
        result = await svc.get_fundamentals("AAPL")
    assert result["data_source"] == "yfinance"
    assert result["market_cap"] == 3e12


@pytest.mark.asyncio
async def test_get_fundamentals_marks_data_source_unavailable_when_both_fail():
    """yfinance.get_info raised on top of no Polygon key ⇒ unavailable."""
    with patch.object(svc.settings, "POLYGON_API_KEY", ""), \
         patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()) as mock_set, \
         patch.object(svc.yfinance, "get_info",
                      AsyncMock(side_effect=RuntimeError("blocked"))):
        result = await svc.get_fundamentals("AAPL")
    assert result["data_source"] == "unavailable"
    # Empty payload should not be cached.
    mock_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_screener_yfinance_concurrency_capped():
    """At most _SCREENER_INFO_CONCURRENCY (4) `.info` calls may be in
    flight simultaneously, even though the outer batch_size is 20.
    Without this cap a screener pass starves the shared yfinance
    ThreadPool and ad-hoc /quote requests stall behind it."""
    in_flight = 0
    peak = 0
    lock = svc.asyncio.Lock()

    async def slow_info(_t: str):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await svc.asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return {"marketCap": 1e12, "trailingPE": 20, "priceToBook": 5,
                "regularMarketPrice": 100, "regularMarketChangePercent": 0.5,
                "longName": "X", "sector": "Tech", "volume": 1_000_000}

    with patch.object(svc, "cache_get", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set", AsyncMock()), \
         patch.object(svc.yfinance, "get_info", new=slow_info), \
         patch.object(svc, "_get_sp500_tickers",
                      AsyncMock(return_value=[f"T{i:02d}" for i in range(20)])):
        await svc.get_screener(min_pe=1, limit=20)

    assert peak <= 4, f"screener fan-out exceeded cap: peak={peak}"


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
