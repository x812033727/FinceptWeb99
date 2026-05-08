"""Unit tests for ai/tools/openai_compat.py — verify the new tools added
in PR #346 (`get_options_chain`, `get_symbol_news`, `get_symbol_sentiment`).

The OpenAI-compat path duplicates the Claude Agent toolset by design
(separate JSON schemas vs `@tool` decorators), so we exercise each tool's
dispatch handler independently from `test_ai_tools.py` to catch
regressions where one path is updated without the other.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from ai.tools.openai_compat import build_openai_compat_toolset


def _user_id() -> str:
    return str(uuid.uuid4())


def _payload(s: str) -> dict:
    return json.loads(s)


# ── schema surface ──────────────────────────────────────────────────


def test_toolset_exposes_new_tools_in_dispatch():
    """Adding to schemas without dispatch entries (or vice versa) is the
    classic schema/handler drift bug. Pin the contract."""
    schemas, dispatch = build_openai_compat_toolset(_user_id())
    schema_names = {s["function"]["name"] for s in schemas}
    expected = {
        "get_quote", "run_dcf", "run_var", "run_backtest", "query_user_data",
        "get_options_chain", "get_symbol_news", "get_symbol_sentiment",
        "get_peers", "get_financials",
        "get_institutional_history", "get_margin_history",
        "get_top_brokers", "get_taifex_positioning",
    }
    assert expected <= schema_names
    assert expected <= set(dispatch.keys())


def test_options_chain_schema_requires_only_symbol():
    """`expiration` is optional — absence should produce a valid call
    that defaults to the nearest expiry, so the schema must NOT mark
    it required."""
    schemas, _ = build_openai_compat_toolset(_user_id())
    spec = next(s for s in schemas if s["function"]["name"] == "get_options_chain")
    params = spec["function"]["parameters"]
    assert params["required"] == ["symbol"]
    assert "expiration" in params["properties"]


# ── get_options_chain handler ───────────────────────────────────────


def _contract(strike: float, expiration: str, volume: int = 0, oi: int = 0) -> dict:
    return {
        "ticker": f"NVDA{expiration}{int(strike)}",
        "underlying_ticker": "NVDA",
        "contract_type": "call",
        "expiration_date": expiration,
        "strike_price": strike,
        "last_price": 1.0, "bid": 0.9, "ask": 1.1,
        "volume": volume, "open_interest": oi,
        "implied_volatility": 0.4,
        "in_the_money": False,
    }


@pytest.mark.asyncio
async def test_options_chain_focuses_nearest_when_unspecified():
    _, dispatch = build_openai_compat_toolset(_user_id())
    chain = [
        _contract(100, "2026-06-20", volume=10, oi=5),
        _contract(100, "2026-09-19", volume=999, oi=999),
    ]
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = chain
        result = _payload(await dispatch["get_options_chain"]({"symbol": "NVDA"}))

    assert result["expiration"] == "2026-06-20"
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_options_chain_caps_30_by_liquidity():
    _, dispatch = build_openai_compat_toolset(_user_id())
    chain = [
        _contract(strike=k, expiration="2026-06-20", volume=k, oi=k)
        for k in range(1, 51)
    ]
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = chain
        result = _payload(await dispatch["get_options_chain"]({
            "symbol": "NVDA", "expiration": "2026-06-20",
        }))

    assert result["count"] == 30
    strikes = {c["strike_price"] for c in result["contracts"]}
    assert 50 in strikes  # most liquid retained
    assert 1 not in strikes  # least liquid pruned


@pytest.mark.asyncio
async def test_options_chain_empty_returns_safe_payload():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        result = _payload(await dispatch["get_options_chain"]({"symbol": "DELISTED"}))
    assert result["count"] == 0
    assert result["contracts"] == []


@pytest.mark.asyncio
async def test_options_chain_service_failure_surfaces_error():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.side_effect = RuntimeError("upstream timeout")
        result = _payload(await dispatch["get_options_chain"]({"symbol": "NVDA"}))
    assert "error" in result


# ── get_symbol_news handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_symbol_news_routes_us_and_caps_limit():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.us_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = [{"title": "AAPL beats Q1", "link": "https://x"}]
        result = _payload(await dispatch["get_symbol_news"]({
            "symbol": "aapl", "market": "US", "limit": 999,
        }))
    mock.assert_awaited_once_with("AAPL", limit=20)
    assert result["market"] == "US"


@pytest.mark.asyncio
async def test_symbol_news_routes_tw_default_limit():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch("services.tw_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        await dispatch["get_symbol_news"]({"symbol": "2330", "market": "tw"})
    mock.assert_awaited_once_with("2330", limit=10)


@pytest.mark.asyncio
async def test_symbol_news_rejects_unknown_market():
    _, dispatch = build_openai_compat_toolset(_user_id())
    result = _payload(await dispatch["get_symbol_news"]({
        "symbol": "X", "market": "JP",
    }))
    assert "error" in result


# ── get_symbol_sentiment handler ────────────────────────────────────


@pytest.mark.asyncio
async def test_symbol_sentiment_returns_aggregate():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = {
            "symbol": "2330", "considered": 12, "count": 12,
            "avg_score": 0.42, "bullish": 8, "bearish": 1, "neutral": 3,
            "headlines": [],
        }
        result = _payload(await dispatch["get_symbol_sentiment"]({
            "symbol": "2330", "market": "TW",
        }))
    assert result["avg_score"] == 0.42
    assert result["bullish"] == 8


@pytest.mark.asyncio
async def test_symbol_sentiment_no_scored_articles_returns_zero():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        result = _payload(await dispatch["get_symbol_sentiment"]({
            "symbol": "OBSCURE", "market": "US",
        }))
    assert result["considered"] == 0
    assert result["headlines"] == []


@pytest.mark.asyncio
async def test_symbol_sentiment_max_age_clamped():
    """`max_age_hours` > 720 (30 days) silently clamped — protects
    against an LLM accidentally asking for the full sentiment history."""
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        await dispatch["get_symbol_sentiment"]({
            "symbol": "X", "market": "US", "max_age_hours": 99999,
        })
    args = mock.await_args.kwargs
    assert args["max_age_hours"] == 720


@pytest.mark.asyncio
async def test_symbol_sentiment_rejects_unknown_market():
    _, dispatch = build_openai_compat_toolset(_user_id())
    result = _payload(await dispatch["get_symbol_sentiment"]({
        "symbol": "X", "market": "JP",
    }))
    assert "error" in result


# ── get_peers handler ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_peers_us_not_supported_yet():
    """US peers explicitly unsupported (no curated industry mapping).
    Tool must return an explicit error, not a stale fallback list."""
    _, dispatch = build_openai_compat_toolset(_user_id())
    result = _payload(await dispatch["get_peers"]({
        "symbol": "AAPL", "market": "US",
    }))
    assert "error" in result
    assert "TW only" in result["error"]


@pytest.mark.asyncio
async def test_peers_tw_returns_quote_enriched_rows():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.tw_market_service.get_industry_peers",
        return_value=["2454", "2330"],
    ), patch(
        "services.tw_market_service.get_industry",
        return_value="半導體業",
    ), patch(
        "services.tw_market_service.get_company_name",
        side_effect=lambda s: {"2454": "聯發科", "2330": "台積電"}.get(s),
    ), patch(
        "services.tw_market_service.get_quote",
        new_callable=AsyncMock,
    ) as q_mock:
        q_mock.side_effect = [
            {"name_zh": "聯發科", "price": 1200.0, "change_pct": 1.5,
             "pe_ratio": 18.0, "pb_ratio": 4.0, "dividend_yield": 3.0},
            {"name_zh": "台積電", "price": 1100.0, "change_pct": 2.0,
             "pe_ratio": 25.0, "pb_ratio": 6.0, "dividend_yield": 1.8},
        ]
        result = _payload(await dispatch["get_peers"]({
            "symbol": "2308", "market": "TW", "limit": 5,
        }))

    assert result["industry"] == "半導體業"
    assert result["count"] == 2
    syms = {p["symbol"] for p in result["peers"]}
    assert syms == {"2454", "2330"}


@pytest.mark.asyncio
async def test_peers_tw_caps_limit_at_10():
    _, dispatch = build_openai_compat_toolset(_user_id())
    big_peer_list = [f"23{i:02d}" for i in range(50)]
    with patch(
        "services.tw_market_service.get_industry_peers",
        return_value=big_peer_list,
    ), patch(
        "services.tw_market_service.get_industry", return_value="x",
    ), patch(
        "services.tw_market_service.get_company_name", return_value=None,
    ), patch(
        "services.tw_market_service.get_quote",
        new_callable=AsyncMock,
    ) as q_mock:
        q_mock.return_value = {}
        result = _payload(await dispatch["get_peers"]({
            "symbol": "2308", "market": "TW", "limit": 999,
        }))
    # 999 must be clipped to 10 (the schema-level max).
    assert result["count"] == 10


@pytest.mark.asyncio
async def test_peers_tw_no_industry_returns_empty():
    """A symbol whose 產業別 hasn't loaded yields an empty peer list
    instead of crashing — the daily symbol-map cron may not have run
    on a fresh deploy."""
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.tw_market_service.get_industry_peers", return_value=[],
    ), patch(
        "services.tw_market_service.get_industry", return_value=None,
    ):
        result = _payload(await dispatch["get_peers"]({
            "symbol": "9999", "market": "TW",
        }))
    assert result["peers"] == []
    assert result["industry"] is None


# ── get_financials handler ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_financials_us_caps_5_periods_per_statement():
    _, dispatch = build_openai_compat_toolset(_user_id())
    big = [{"period": f"FY{2026 - i}", "revenue": 100 + i} for i in range(15)]
    with patch(
        "services.us_market_service.get_financials",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = {
            "source": "yfinance",
            "income_statement": big,
            "balance_sheet": big,
            "cash_flow": big,
        }
        result = _payload(await dispatch["get_financials"]({
            "symbol": "AAPL", "market": "US",
        }))
    assert result["source"] == "yfinance"
    assert len(result["income_statement"]) == 5
    assert len(result["balance_sheet"]) == 5
    assert len(result["cash_flow"]) == 5


@pytest.mark.asyncio
async def test_financials_tw_caps_60_rows():
    _, dispatch = build_openai_compat_toolset(_user_id())
    big = [{"date": f"2026-{i:02d}-01", "type": "Revenue", "value": i}
           for i in range(1, 200)]
    with patch(
        "services.tw_market_service.get_financials",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = big
        result = _payload(await dispatch["get_financials"]({
            "symbol": "2330", "market": "TW",
        }))
    # Most recent 60 rows preserved (tail slice).
    assert len(result["rows"]) == 60
    assert result["rows"][-1]["value"] == 199
    assert result["count"] == 199


@pytest.mark.asyncio
async def test_financials_rejects_unknown_market():
    _, dispatch = build_openai_compat_toolset(_user_id())
    result = _payload(await dispatch["get_financials"]({
        "symbol": "X", "market": "JP",
    }))
    assert "error" in result


# ── get_institutional_history handler ───────────────────────────────


@pytest.mark.asyncio
async def test_institutional_history_passes_days_to_service():
    _, dispatch = build_openai_compat_toolset(_user_id())
    fake_rows = [
        {"date": f"2026-04-{i:02d}", "foreign_buy": 1000 * i}
        for i in range(1, 11)
    ]
    with patch(
        "services.tw_market_service.get_institutional",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = fake_rows
        result = _payload(await dispatch["get_institutional_history"]({
            "symbol": "2330", "days": 60,
        }))
    mock.assert_awaited_once_with("2330", days=60)
    assert result["count"] == 10
    assert result["days"] == 60


@pytest.mark.asyncio
async def test_institutional_history_caps_days_at_90():
    """A persona that requests days=999 must not blow up the upstream
    range scan — clamp at 90."""
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.tw_market_service.get_institutional",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = []
        await dispatch["get_institutional_history"]({
            "symbol": "2330", "days": 999,
        })
    mock.assert_awaited_once_with("2330", days=90)


# ── get_margin_history handler ──────────────────────────────────────


@pytest.mark.asyncio
async def test_margin_history_passes_through_rows():
    _, dispatch = build_openai_compat_toolset(_user_id())
    fake_rows = [
        {"date": "2026-04-30", "margin_purchase_balance": 50000,
         "short_sale_balance": 12000},
    ]
    with patch(
        "services.tw_market_service.get_margin",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = fake_rows
        result = _payload(await dispatch["get_margin_history"]({
            "symbol": "2330",
        }))
    mock.assert_awaited_once_with("2330", days=30)
    assert result["rows"] == fake_rows


# ── get_top_brokers handler ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_top_brokers_returns_payload_when_data_exists():
    _, dispatch = build_openai_compat_toolset(_user_id())
    fake = {
        "symbol": "2330", "as_of": "2026-04-30",
        "from_ts": "2026-04-23", "session_count": 5,
        "top_buyers": [{"broker": "凱基台北", "broker_id": "9200",
                        "net_buy_shares": 12345}],
        "top_sellers": [{"broker": "美林", "broker_id": "1470",
                         "net_buy_shares": -8000}],
    }
    with patch(
        "services.broker_concentration_service.get_top_brokers_for_symbol",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = fake
        result = _payload(await dispatch["get_top_brokers"]({
            "symbol": "2330", "days": 5, "top_n": 3,
        }))
    mock.assert_awaited_once_with("2330", days=5, top_n=3)
    assert result["top_buyers"][0]["broker"] == "凱基台北"


@pytest.mark.asyncio
async def test_top_brokers_no_data_returns_structured_note():
    """`get_top_brokers_for_symbol` returns None for thinly-traded
    symbols / FinMind quota exhaustion. The tool surfaces that as a
    structured note instead of an error so the LLM doesn't retry."""
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.broker_concentration_service.get_top_brokers_for_symbol",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        result = _payload(await dispatch["get_top_brokers"]({"symbol": "9999"}))
    assert result["top_buyers"] == []
    assert "note" in result


@pytest.mark.asyncio
async def test_top_brokers_caps_top_n_at_10():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.broker_concentration_service.get_top_brokers_for_symbol",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        await dispatch["get_top_brokers"]({"symbol": "2330", "top_n": 99})
    mock.assert_awaited_once_with("2330", days=5, top_n=10)


# ── get_taifex_positioning handler ──────────────────────────────────


@pytest.mark.asyncio
async def test_taifex_positioning_default_contract_tx():
    _, dispatch = build_openai_compat_toolset(_user_id())
    fake = {
        "contract": "TX", "as_of": "2026-04-30", "session_count": 5,
        "fini": {"net_oi": 50000, "change_5d": 10000},
        "sitc": {"net_oi": 2000, "change_5d": -500},
        "dealer": {"net_oi": -3000, "change_5d": 1000},
        "trend": "bullish",
    }
    with patch(
        "services.derivatives_service.get_taifex_positioning",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = fake
        result = _payload(await dispatch["get_taifex_positioning"]({}))
    mock.assert_awaited_once_with(contract="TX")
    assert result["trend"] == "bullish"


@pytest.mark.asyncio
async def test_taifex_positioning_no_data_returns_structured_note():
    _, dispatch = build_openai_compat_toolset(_user_id())
    with patch(
        "services.derivatives_service.get_taifex_positioning",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        result = _payload(await dispatch["get_taifex_positioning"]({
            "contract": "MTX",
        }))
    assert result["contract"] == "MTX"
    assert "note" in result
