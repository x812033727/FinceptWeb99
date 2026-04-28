"""Read-tier tests for tw_market_service.get_history.

Verifies the new Redis → DB → upstream → stale-DB ordering. The
repository's autosession helpers are mocked so the test exercises
control flow without needing a real DB or Redis.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as tw


def _bars(start: str, end: str) -> list[dict]:
    """Tiny helper — two bars with `time` set to start and end ISO strings."""
    return [
        {"time": start, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1_000},
        {"time": end,   "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 1_100},
    ]


@pytest.fixture
def patch_io():
    """Stub out everything tw.get_history touches: cache, DB, upstream."""
    with patch.object(tw, "cache_get", AsyncMock(return_value=None)) as cache_get, \
         patch.object(tw, "cache_set", AsyncMock()) as cache_set, \
         patch("services.ingest.repository.read_ohlcv_range_autosession",
               AsyncMock(return_value=[])) as db_read, \
         patch("services.ingest.repository.upsert_ohlcv_bars_autosession",
               AsyncMock(return_value=0)) as db_write, \
         patch.object(tw.twse, "get_daily_ohlcv", AsyncMock(return_value=[])) as twse_hist, \
         patch.object(tw.finmind, "get_daily_ohlcv", AsyncMock(return_value=[])) as finmind_hist:
        yield {
            "cache_get": cache_get, "cache_set": cache_set,
            "db_read": db_read, "db_write": db_write,
            "twse_hist": twse_hist, "finmind_hist": finmind_hist,
        }


@pytest.mark.asyncio
async def test_redis_hit_skips_db_and_upstream(patch_io):
    import json
    cached_bars = _bars("2026-04-01", "2026-04-02")
    patch_io["cache_get"].return_value = json.dumps(cached_bars)

    out = await tw.get_history("2330", months=1)

    assert out == cached_bars
    patch_io["db_read"].assert_not_awaited()
    patch_io["twse_hist"].assert_not_awaited()
    patch_io["finmind_hist"].assert_not_awaited()


@pytest.mark.asyncio
async def test_db_fresh_skips_upstream(patch_io):
    """Last DB bar within freshness window → return DB bars, never call TWSE."""
    from datetime import date, timedelta
    today = date.today()
    yesterday = today - timedelta(days=1)
    fresh_bars = _bars((today - timedelta(days=10)).isoformat(), yesterday.isoformat())
    patch_io["db_read"].return_value = fresh_bars

    out = await tw.get_history("2330", months=1)

    assert out == fresh_bars
    patch_io["twse_hist"].assert_not_awaited()
    patch_io["finmind_hist"].assert_not_awaited()
    patch_io["cache_set"].assert_awaited_once()


@pytest.mark.asyncio
async def test_db_stale_falls_through_to_upstream(patch_io):
    """Last DB bar > 5 days old → upstream is called and result is written back."""
    from datetime import date, timedelta
    today = date.today()
    stale = _bars(
        (today - timedelta(days=30)).isoformat(),
        (today - timedelta(days=20)).isoformat(),
    )
    fresh_upstream = _bars(
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
    )
    patch_io["db_read"].return_value = stale
    patch_io["twse_hist"].return_value = fresh_upstream

    out = await tw.get_history("2330", months=1)

    assert out == fresh_upstream
    patch_io["twse_hist"].assert_awaited()
    patch_io["db_write"].assert_awaited_once()
    patch_io["cache_set"].assert_awaited()


@pytest.mark.asyncio
async def test_upstream_failure_returns_stale_db(patch_io):
    """Upstream fully fails AND DB has stale data → serve stale, don't return []."""
    from datetime import date, timedelta
    today = date.today()
    stale = _bars(
        (today - timedelta(days=30)).isoformat(),
        (today - timedelta(days=20)).isoformat(),
    )
    patch_io["db_read"].return_value = stale
    patch_io["twse_hist"].side_effect = RuntimeError("twse blocked")
    patch_io["finmind_hist"].side_effect = RuntimeError("finmind blocked")

    out = await tw.get_history("2330", months=1)

    assert out == stale
    patch_io["db_write"].assert_not_awaited()


@pytest.mark.asyncio
async def test_db_empty_and_upstream_empty_returns_empty(patch_io):
    out = await tw.get_history("UNKNOWN", months=1)
    assert out == []
    patch_io["db_write"].assert_not_awaited()
    patch_io["cache_set"].assert_not_awaited()


@pytest.mark.asyncio
async def test_twse_empty_falls_through_to_finmind(patch_io):
    """Original waterfall behaviour preserved when DB is empty."""
    from datetime import date, timedelta
    today = date.today()
    finmind_bars = _bars(
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
    )
    patch_io["finmind_hist"].return_value = finmind_bars

    out = await tw.get_history("9999", months=1)

    assert out == finmind_bars
    patch_io["finmind_hist"].assert_awaited()
    patch_io["db_write"].assert_awaited_once()


@pytest.mark.asyncio
async def test_db_read_error_does_not_break_request(patch_io):
    """A DB error in `read_ohlcv_range_autosession` already returns []
    (the wrapper swallows exceptions). The service must not crash and
    must still serve the upstream waterfall.
    """
    from datetime import date, timedelta
    today = date.today()
    fresh = _bars(
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
    )
    # autosession wrapper returns [] on DB error — simulate by leaving
    # db_read.return_value as the default [] but having upstream succeed.
    patch_io["twse_hist"].return_value = fresh

    out = await tw.get_history("2330", months=1)
    assert out == fresh
