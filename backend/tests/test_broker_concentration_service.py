"""Tests for `services.broker_concentration_service` (PR #285).

Stubs the FinMind connector + Redis cache so the test exercises
just the aggregation / sort / cap / cache logic without booting
the live API.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from services import broker_concentration_service as svc


def _row(
    *,
    broker: str,
    broker_id: str = "9200",
    buy: int | None = 0,
    sell: int | None = 0,
    ts: str = "2026-04-15",
) -> dict:
    return {
        "date":                 ts,
        "stock_id":             "2330",
        "securities_trader":    broker,
        "securities_trader_id": broker_id,
        "buy":                  buy,
        "sell":                 sell,
    }


# ── get_top_brokers_for_symbol ───────────────────────────────────


@pytest.mark.asyncio
async def test_aggregates_per_broker_over_window():
    """Same broker on multiple days has its rows summed into one
    aggregated entry — not duplicated."""
    rows = [
        _row(broker="凱基台北", buy=10000, sell=2000, ts="2026-04-11"),
        _row(broker="凱基台北", buy=20000, sell=5000, ts="2026-04-12"),
        _row(broker="元大林口", buy=3000,  sell=1000, ts="2026-04-11"),
    ]
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               new=AsyncMock(return_value=rows)), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_top_brokers_for_symbol(
            "2330", as_of=date(2026, 4, 15),
        )

    assert out is not None
    # 凱基台北: buy 30000 - sell 7000 = +23000
    # 元大林口: buy 3000 - sell 1000 = +2000
    by_broker = {b["broker"]: b for b in out["top_buyers"]}
    assert by_broker["凱基台北"]["net_buy_shares"] == 23000
    assert by_broker["元大林口"]["net_buy_shares"] == 2000
    # Sorted desc by net_buy_shares
    assert out["top_buyers"][0]["broker"] == "凱基台北"


@pytest.mark.asyncio
async def test_separates_top_buyers_from_top_sellers():
    """Negative net buy → goes to top_sellers (most-negative first).
    Positive → top_buyers (largest first). Zero net excluded."""
    rows = [
        _row(broker="買方甲", buy=20000, sell=2000),   # +18000
        _row(broker="買方乙", buy=15000, sell=5000),   # +10000
        _row(broker="持平丙", buy=3000,  sell=3000),   # 0 — excluded
        _row(broker="賣方丁", buy=1000,  sell=21000),  # -20000
        _row(broker="賣方戊", buy=2000,  sell=12000),  # -10000
    ]
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               new=AsyncMock(return_value=rows)), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_top_brokers_for_symbol(
            "2330", as_of=date(2026, 4, 15),
        )

    assert out is not None
    # Top buyers ordered most-positive first.
    assert [b["broker"] for b in out["top_buyers"]] == ["買方甲", "買方乙"]
    # Top sellers ordered most-negative first.
    assert [b["broker"] for b in out["top_sellers"]] == ["賣方丁", "賣方戊"]
    # Persistent zero-net brokers excluded from both lists.
    syms = {b["broker"] for b in out["top_buyers"] + out["top_sellers"]}
    assert "持平丙" not in syms


@pytest.mark.asyncio
async def test_caps_at_top_n():
    """Default top_n=3 → only 3 buyers + 3 sellers in the result
    even when many brokers exceed the threshold."""
    rows = [
        _row(broker=f"B{i}", buy=10000 - i * 100, sell=0)
        for i in range(8)
    ]
    rows.extend([
        _row(broker=f"S{i}", buy=0, sell=10000 - i * 100)
        for i in range(8)
    ])
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               new=AsyncMock(return_value=rows)), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_top_brokers_for_symbol(
            "2330", as_of=date(2026, 4, 15), top_n=3,
        )
    assert len(out["top_buyers"]) == 3
    assert len(out["top_sellers"]) == 3
    # Top buyer is B0 (largest buy).
    assert out["top_buyers"][0]["broker"] == "B0"
    # Top seller is S0 (largest sell).
    assert out["top_sellers"][0]["broker"] == "S0"


@pytest.mark.asyncio
async def test_clamps_to_as_of_in_backtest_mode():
    """Rows post-anchor must be excluded — backtest at as_of=2026-04-15
    must NOT see broker activity from 2026-04-22 even if FinMind
    returns it (e.g. when the call window happens to include it)."""
    rows = [
        _row(broker="凱基台北", buy=5000, sell=0, ts="2026-04-11"),
        _row(broker="凱基台北", buy=10000, sell=0, ts="2026-04-12"),
        _row(broker="凱基台北", buy=99999, sell=0, ts="2026-04-22"),  # post-anchor
    ]
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               new=AsyncMock(return_value=rows)), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_top_brokers_for_symbol(
            "2330", as_of=date(2026, 4, 15),
        )
    # +5000 + +10000 = 15000; the 99999 from post-anchor must NOT
    # contribute.
    assert out["top_buyers"][0]["net_buy_shares"] == 15000


@pytest.mark.asyncio
async def test_returns_none_on_empty_finmind_response():
    """FinMind returns nothing for the symbol → service returns
    None and writes a sentinel cache entry."""
    set_mock = AsyncMock()
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               new=AsyncMock(return_value=[])), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               set_mock):
        out = await svc.get_top_brokers_for_symbol(
            "9999", as_of=date(2026, 4, 15),
        )
    assert out is None
    # Sentinel cache write so we don't keep re-fetching.
    assert set_mock.call_count == 1
    assert "_no_data" in set_mock.call_args.args[1]


@pytest.mark.asyncio
async def test_returns_none_on_connector_failure():
    """yfinance / FinMind raising must NOT propagate — service
    returns None so the ctx block silently drops the symbol."""
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               new=AsyncMock(side_effect=RuntimeError("FinMind 502"))), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_top_brokers_for_symbol(
            "2330", as_of=date(2026, 4, 15),
        )
    assert out is None


@pytest.mark.asyncio
async def test_short_circuits_on_cache_hit():
    """Cached entry skips the FinMind call entirely."""
    import json as _json
    cached_payload = {
        "symbol": "2330", "as_of": "2026-04-15",
        "from_ts": "2026-04-11", "session_count": 5,
        "top_buyers": [{
            "broker": "凱基台北", "broker_id": "9200",
            "net_buy_shares": 12345,
        }],
        "top_sellers": [],
    }
    fetch_mock = AsyncMock()
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               fetch_mock), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=_json.dumps(cached_payload))):
        out = await svc.get_top_brokers_for_symbol(
            "2330", as_of=date(2026, 4, 15),
        )
    assert out == cached_payload
    fetch_mock.assert_not_called()


# ── get_broker_concentration_for_focus ───────────────────────────


@pytest.mark.asyncio
async def test_for_focus_caps_per_discussion_fan_out():
    """Topic mentioning 8 stocks must NOT fan out 8 FinMind calls
    — service caps at `max_symbols=5` to bound Sponsor quota
    burn per discussion."""
    fetch_mock = AsyncMock(return_value=[
        _row(broker="凱基台北", buy=1000),
    ])
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               fetch_mock), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_broker_concentration_for_focus(
            ["2330", "2454", "2317", "2603", "2891", "2882", "2880", "2308"],
            as_of=date(2026, 4, 15),
        )
    assert len(out) == 5
    assert fetch_mock.call_count == 5


@pytest.mark.asyncio
async def test_for_focus_drops_synthetic_and_empty_symbols():
    """Synthetic `_TAIEX*` and empty-string symbols are filtered
    BEFORE the cap counts them — five real focus_symbols still
    yields 5 fetches even when extras are filtered."""
    fetch_mock = AsyncMock(return_value=[
        _row(broker="凱基台北", buy=1000),
    ])
    with patch("data.tw.finmind_connector.get_broker_concentration_per_symbol",
               fetch_mock), \
         patch("services.broker_concentration_service.cache_get",
               new=AsyncMock(return_value=None)), \
         patch("services.broker_concentration_service.cache_set",
               new=AsyncMock()):
        out = await svc.get_broker_concentration_for_focus(
            ["_TAIEX", "2330", "", "2454"],
            as_of=date(2026, 4, 15),
        )
    # 2 real symbols → 2 fetches (synthetic + empty filtered).
    assert len(out) == 2
    assert fetch_mock.call_count == 2


@pytest.mark.asyncio
async def test_for_focus_empty_input_returns_empty():
    out = await svc.get_broker_concentration_for_focus([])
    assert out == []
    out = await svc.get_broker_concentration_for_focus(None)
    assert out == []
