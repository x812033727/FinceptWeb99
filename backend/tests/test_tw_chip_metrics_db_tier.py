"""Read-tier tests for tw_market_service chip-metric paths.

Verifies that `get_institutional` / `get_margin` consult the DB
archive (`tw_institutional_daily` / `tw_margin_daily`) before
calling FinMind / TWSE. Repository helpers are mocked so the tests
exercise control flow without needing a real DB or Redis.
"""
from unittest.mock import AsyncMock, patch

import pytest

import services.tw_market_service as tw


def _without_source(rows: list[dict]) -> list[dict]:
    return [{key: value for key, value in row.items() if key != "data_source"} for row in rows]


def _institutional_db_rows() -> list[dict]:
    return [
        {
            "date": "2026-04-29", "symbol": "2330",
            "fini_buy": 50_000, "fini_sell": 10_000,
            "sitc_buy": 1_000, "sitc_sell": 500,
            "dealer_buy": 200, "dealer_sell": 100,
        },
    ]


def _margin_db_rows() -> list[dict]:
    return [
        {
            "date": "2026-04-29", "symbol": "2330",
            "margin_purchase": 5_000, "margin_balance": 20_000,
            "short_sale": 1_000, "short_balance": 3_000,
        },
    ]


@pytest.mark.asyncio
async def test_institutional_db_hit_short_circuits_upstream():
    """When the DB archive has rows for the requested range, neither
    FinMind nor TWSE should be called."""
    with patch.object(tw, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(tw, "cache_set_json", AsyncMock()), \
         patch("services.ingest.repository.read_institutional_range",
               AsyncMock(return_value=_institutional_db_rows())) as db_read, \
         patch.object(tw.finmind, "get_institutional",
                      AsyncMock()) as finmind, \
         patch.object(tw.twse, "get_institutional",
                      AsyncMock()) as twse_call:
        result = await tw.get_institutional("2330", days=30)

    assert _without_source(result) == _institutional_db_rows()
    assert result[0]["data_source"] == "postgres"
    db_read.assert_awaited_once()
    finmind.assert_not_awaited()
    twse_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_institutional_db_miss_falls_through_to_finmind():
    """Empty DB → FinMind. TWSE only fires if FinMind also returned
    nothing."""
    finmind_rows = [{"date": "2026-04-29", "symbol": "2330", "fini_buy": 1}]
    with patch.object(tw, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(tw, "cache_set_json", AsyncMock()), \
         patch("services.ingest.repository.read_institutional_range",
               AsyncMock(return_value=[])), \
         patch.object(tw.finmind, "get_institutional",
                      AsyncMock(return_value=finmind_rows)) as finmind, \
         patch.object(tw.twse, "get_institutional",
                      AsyncMock()) as twse_call:
        result = await tw.get_institutional("2330", days=30)

    assert _without_source(result) == finmind_rows
    assert result[0]["data_source"] == "finmind"
    finmind.assert_awaited_once()
    twse_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_margin_db_hit_short_circuits_upstream():
    with patch.object(tw, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(tw, "cache_set_json", AsyncMock()), \
         patch("services.ingest.repository.read_margin_range",
               AsyncMock(return_value=_margin_db_rows())) as db_read, \
         patch.object(tw.finmind, "get_margin",
                      AsyncMock()) as finmind, \
         patch.object(tw.twse, "get_margin",
                      AsyncMock()) as twse_call:
        result = await tw.get_margin("2330", days=30)

    assert _without_source(result) == _margin_db_rows()
    assert result[0]["data_source"] == "postgres"
    db_read.assert_awaited_once()
    finmind.assert_not_awaited()
    twse_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_margin_db_outage_does_not_hide_upstream():
    """Repository raises (e.g. transient DB outage) → fall through to
    upstream rather than returning empty. Resilience contract."""
    finmind_rows = [{"date": "2026-04-29", "symbol": "2330", "margin_balance": 1}]
    with patch.object(tw, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(tw, "cache_set_json", AsyncMock()), \
         patch("services.ingest.repository.read_margin_range",
               AsyncMock(side_effect=RuntimeError("db down"))), \
         patch.object(tw.finmind, "get_margin",
                      AsyncMock(return_value=finmind_rows)):
        result = await tw.get_margin("2330", days=30)

    assert _without_source(result) == finmind_rows
    assert result[0]["data_source"] == "finmind"
