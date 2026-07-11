"""
Tests for ai/tools/screener.py — the run_screener NL screening tool (功能 B2).

Covers: filter correctness on stubbed screener data, limit clamping,
constrained-schema rejection (read-only guard), the TW 外資買超 DB
path (whitelisted ORM query), and registration in both tool loops.
"""
import json
from datetime import date, timedelta

import pytest
from unittest.mock import AsyncMock, patch

from ai.tools.screener import make_screener_tools, run_screener_query


def _handler(tool_obj):
    return tool_obj.handler


def _text(mcp_result: dict) -> dict:
    return json.loads(mcp_result["content"][0]["text"])


def _us_row(symbol: str, **over) -> dict:
    row = {
        "symbol": symbol, "market": "US", "name": f"{symbol} Inc",
        "price": 100.0, "change_pct": 1.0, "volume": 1_000_000,
        "market_cap": 5e10, "pe_ratio": 20.0, "pb_ratio": 3.0,
        "dividend_yield": 1.5, "sector": "Technology",
        "data_source": "test",
    }
    row.update(over)
    return row


def _tw_row(symbol: str, **over) -> dict:
    row = {
        "symbol": symbol, "market": "TW", "exchange": "TWSE",
        "name_zh": f"公司{symbol}", "price": 50.0, "change": 0.5,
        "change_pct": 1.0, "volume": 2_000_000, "pe_ratio": 12.0,
        "pb_ratio": 1.5, "dividend_yield": 4.0, "data_source": "twse",
    }
    row.update(over)
    return row


# ── filter correctness on stubbed data ──────────────────────────────

@pytest.mark.asyncio
async def test_us_price_and_change_filters_applied_in_python():
    stub = [
        _us_row("AAA", price=10.0, change_pct=-2.0),
        _us_row("BBB", price=50.0, change_pct=3.0),
        _us_row("CCC", price=500.0, change_pct=5.0),
        _us_row("DDD", price=60.0, change_pct=None),  # missing field fails bound
    ]
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({
            "market": "US",
            "filters": {"price_min": 20, "price_max": 100, "change_pct_min": 0},
        })
    assert [r["symbol"] for r in result["rows"]] == ["BBB"]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_us_fundamental_filters_pushed_down_to_service():
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        await run_screener_query({
            "market": "US",
            "filters": {"pe_max": 15, "dividend_yield_min": 3,
                        "market_cap_min": 1e10, "sector": "Energy",
                        "volume_min": 500000},
        })
    kwargs = mock.await_args.kwargs
    assert kwargs["max_pe"] == 15
    assert kwargs["min_dividend_yield"] == 3
    assert kwargs["min_market_cap"] == 1e10
    assert kwargs["sector"] == "Energy"
    assert kwargs["min_volume"] == 500000


@pytest.mark.asyncio
async def test_tw_etf_flags_pushed_down():
    with patch("services.tw_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        await run_screener_query({
            "market": "TW",
            "filters": {"exclude_etf": True, "pe_max": 20},
        })
    kwargs = mock.await_args.kwargs
    assert kwargs["include_etf"] is False
    assert kwargs["etf_only"] is False
    assert kwargs["max_pe"] == 20


@pytest.mark.asyncio
async def test_sort_by_pe_ascending_and_missing_values_last():
    stub = [
        _us_row("HIGH", pe_ratio=40.0),
        _us_row("LOW", pe_ratio=8.0),
        _us_row("NONE", pe_ratio=None),
        _us_row("MID", pe_ratio=15.0),
    ]
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({
            "market": "US", "sort_by": "pe_ratio",
        })
    assert [r["symbol"] for r in result["rows"]] == ["LOW", "MID", "HIGH", "NONE"]


@pytest.mark.asyncio
async def test_default_sort_volume_descending():
    stub = [_us_row("A", volume=10), _us_row("B", volume=999), _us_row("C", volume=50)]
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({"market": "US"})
    assert [r["symbol"] for r in result["rows"]] == ["B", "C", "A"]
    assert result["sort_by"] == "volume"


@pytest.mark.asyncio
async def test_tw_industry_substring_filter():
    stub = [_tw_row("2330"), _tw_row("2317"), _tw_row("2881")]
    industries = {"2330": "半導體業", "2317": "電子零組件業", "2881": "金融保險業"}
    with patch("services.tw_market_service.get_screener",
               new_callable=AsyncMock) as mock, \
         patch("services.tw_market_service.get_industry",
               side_effect=lambda s: industries.get(s)):
        mock.return_value = stub
        result = await run_screener_query({
            "market": "TW", "filters": {"industry": "電子"},
        })
    assert [r["symbol"] for r in result["rows"]] == ["2317"]


# ── limit clamp ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_clamped_to_50():
    stub = [_us_row(f"S{i:03d}", volume=i) for i in range(80)]
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({"market": "US", "limit": 999})
    assert result["count"] == 50
    assert result["limit"] == 50


@pytest.mark.asyncio
async def test_limit_defaults_to_20():
    stub = [_us_row(f"S{i:03d}", volume=i) for i in range(80)]
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({"market": "US"})
    assert result["count"] == 20


# ── read-only guard: constrained schema rejection ───────────────────

@pytest.mark.asyncio
async def test_unknown_filter_key_rejected():
    """No pass-through: keys outside the whitelist are refused, never
    forwarded anywhere (the SQL-injection-shaped key never reaches a query)."""
    result = await run_screener_query({
        "market": "TW",
        "filters": {"symbol; DROP TABLE users": 1},
    })
    assert "error" in result
    assert "Unknown filter" in result["error"]
    assert "foreign_net_buy_days_min" in result["error"]  # allowed list surfaced


@pytest.mark.asyncio
async def test_us_only_filter_rejected_on_tw():
    result = await run_screener_query({
        "market": "TW", "filters": {"sector": "Technology"},
    })
    assert "error" in result


@pytest.mark.asyncio
async def test_non_numeric_filter_value_rejected():
    result = await run_screener_query({
        "market": "TW",
        "filters": {"foreign_net_buy_days_min": "10 OR 1=1"},
    })
    assert "error" in result
    assert "foreign_net_buy_days_min" in result["error"]


@pytest.mark.asyncio
async def test_unknown_market_rejected():
    result = await run_screener_query({"market": "JP"})
    assert "error" in result


@pytest.mark.asyncio
async def test_invalid_sort_by_rejected():
    result = await run_screener_query({"market": "US", "sort_by": "price; --"})
    assert "error" in result


@pytest.mark.asyncio
async def test_market_cap_sort_rejected_on_tw():
    result = await run_screener_query({"market": "TW", "sort_by": "market_cap"})
    assert "error" in result


# ── TW 外資買超 DB path (whitelisted ORM query) ─────────────────────

@pytest.mark.asyncio
async def test_foreign_net_buy_days_filter(db_session):
    """Seed tw_institutional_daily: 2330 net-bought 12/15 sessions, 2317
    net-sold throughout. foreign_net_buy_days_min=10 keeps only 2330 and
    annotates the row with the computed stats."""
    from models.tw_chip_metrics import TwInstitutionalDaily

    today = date(2026, 7, 10)
    for i in range(15):
        ts = today - timedelta(days=i)
        # 2330: 12 buy days, 3 sell days
        db_session.add(TwInstitutionalDaily(
            market="TW", symbol="2330", ts=ts,
            fini_buy=2_000 if i % 5 != 4 else 100,
            fini_sell=1_000 if i % 5 != 4 else 900,
            source="test",
        ))
        # 2317: always net sell
        db_session.add(TwInstitutionalDaily(
            market="TW", symbol="2317", ts=ts,
            fini_buy=100, fini_sell=5_000, source="test",
        ))
    await db_session.commit()

    stub = [_tw_row("2330"), _tw_row("2317")]
    with patch("services.tw_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({
            "market": "TW",
            "filters": {"foreign_net_buy_days_min": 10},
        })

    assert [r["symbol"] for r in result["rows"]] == ["2330"]
    row = result["rows"][0]
    assert row["foreign_net_buy_days"] == 12
    assert row["foreign_net_shares"] > 0


@pytest.mark.asyncio
async def test_foreign_net_buy_days_empty_archive_returns_empty(db_session):
    stub = [_tw_row("2330")]
    with patch("services.tw_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await run_screener_query({
            "market": "TW",
            "filters": {"foreign_net_buy_days_min": 5},
        })
    assert result["rows"] == []


# ── MCP wrapper + registration in both tool loops ───────────────────

@pytest.mark.asyncio
async def test_mcp_wrapper_returns_text_block():
    tools = {t.name: t for t in make_screener_tools()}
    assert "run_screener" in tools
    stub = [_us_row("AAPL")]
    with patch("services.us_market_service.get_screener",
               new_callable=AsyncMock) as mock:
        mock.return_value = stub
        result = await _handler(tools["run_screener"])({"market": "US"})
    data = _text(result)
    assert data["rows"][0]["symbol"] == "AAPL"


def test_registered_in_claude_agent_tool_names():
    from ai.tools import tool_names
    names = tool_names()
    assert "mcp__fincept__run_screener" in names
    # viewer mode (no user data) keeps the public screener available
    assert "mcp__fincept__run_screener" in tool_names(include_user_data=False)


def test_registered_in_openai_compat_toolset():
    import uuid
    from ai.tools.openai_compat import build_openai_compat_toolset
    schemas, dispatch = build_openai_compat_toolset(str(uuid.uuid4()))
    names = [s["function"]["name"] for s in schemas]
    assert "run_screener" in names
    assert "run_screener" in dispatch
    schema = next(s for s in schemas if s["function"]["name"] == "run_screener")
    props = schema["function"]["parameters"]["properties"]
    # constrained filters schema: additionalProperties must be locked down
    assert props["filters"]["additionalProperties"] is False
    assert props["limit"]["maximum"] == 50


def test_sse_summary_cap_raised_for_run_screener_only():
    from ai.llm_router import _tool_summary_limit
    assert _tool_summary_limit("run_screener") == 24000
    assert _tool_summary_limit("mcp__fincept__run_screener") == 24000
    assert _tool_summary_limit("get_quote") == 2000
    assert _tool_summary_limit("") == 2000
