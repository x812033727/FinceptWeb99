"""
Unit tests for ai/tools/ — verify per-tool behavior without a live Claude.

Each tool is exercised via its handler directly (the @tool decorator
wraps the coroutine but the underlying `handler` is still callable).
"""
import json
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from models.portfolio import Portfolio
from models.watchlist import Watchlist

from ai.tools.financial import make_financial_tools
from ai.tools.sql import make_sql_tools
from ai.tools.web import make_web_tools
from ai.tools.python_exec import make_python_tools


def _handler(tool_obj):
    """SdkMcpTool exposes the coroutine as .handler."""
    return tool_obj.handler


def _text(mcp_result: dict) -> dict:
    """Unwrap an MCP content block into a Python dict (tools all return JSON text)."""
    return json.loads(mcp_result["content"][0]["text"])


# ── financial.get_quote ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_quote_us_market():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.us_market_service.get_quote", new_callable=AsyncMock) as mock:
        mock.return_value = {"symbol": "AAPL", "price": 180.0, "change_pct": 1.2}
        result = await _handler(tools["get_quote"])({"symbol": "aapl", "market": "us"})

    mock.assert_awaited_once_with("AAPL")
    assert _text(result)["price"] == 180.0


@pytest.mark.asyncio
async def test_get_quote_tw_market():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.tw_market_service.get_quote", new_callable=AsyncMock) as mock:
        mock.return_value = {"symbol": "2330", "price": 600.0}
        result = await _handler(tools["get_quote"])({"symbol": "2330", "market": "TW"})

    assert _text(result)["price"] == 600.0


@pytest.mark.asyncio
async def test_get_quote_rejects_unknown_market():
    tools = {t.name: t for t in make_financial_tools()}
    result = await _handler(tools["get_quote"])({"symbol": "AAPL", "market": "JP"})
    assert "error" in _text(result)


@pytest.mark.asyncio
async def test_run_dcf_wraps_service():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.analytics_service.run_dcf_analysis", new_callable=AsyncMock) as mock:
        mock.return_value = {"intrinsic_value": 500, "symbol": "2330", "market": "TW"}
        result = await _handler(tools["run_dcf"])({
            "symbol": "2330", "market": "TW",
            "overrides": {"fcf_history": [1e9], "shares": 1e9},
        })
    assert _text(result)["intrinsic_value"] == 500


@pytest.mark.asyncio
async def test_run_backtest_compacts_equity_curve():
    tools = {t.name: t for t in make_financial_tools()}
    big_curve = [{"date": f"2023-{i:03d}", "value": 100 + i} for i in range(300)]
    with patch("services.analytics_service.run_backtest_analysis", new_callable=AsyncMock) as mock:
        mock.return_value = {"status": "completed", "metrics": {"sharpe": 1.2}, "equity_curve": big_curve}
        result = await _handler(tools["run_backtest"])({
            "symbols": ["AAPL"], "markets": ["US"],
            "strategy": "sma_crossover", "params": {"fast": 10, "slow": 30},
            "start_date": "2023-01-01", "end_date": "2023-12-31",
        })
    data = _text(result)
    assert data["equity_curve"]["count"] == 300
    assert len(data["equity_curve"]["head"]) == 10
    assert len(data["equity_curve"]["tail"]) == 10


# ── sql.query_user_data ─────────────────────────────────────────────

async def _make_user(db: AsyncSession) -> User:
    u = User(email=f"ai_{uuid.uuid4().hex[:8]}@test.com", hashed_password="x", role=UserRole.analyst)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


def _test_session_maker():
    """Build an async_sessionmaker bound to the test engine."""
    from sqlalchemy.ext.asyncio import AsyncSession as _ASession, async_sessionmaker
    from tests.conftest import test_engine
    return async_sessionmaker(test_engine, expire_on_commit=False, class_=_ASession)


@pytest.mark.asyncio
async def test_query_user_data_scopes_by_user(db_session: AsyncSession):
    u1 = await _make_user(db_session)
    u2 = await _make_user(db_session)
    p1 = Portfolio(user_id=u1.id, name="U1 Port", currency="USD")
    p2 = Portfolio(user_id=u2.id, name="U2 Port", currency="USD")
    db_session.add_all([p1, p2])
    await db_session.commit()

    # The tool holds its own `AsyncSessionLocal` reference imported at module load.
    # Patch *that* binding so the tool uses our in-memory SQLite.
    with patch("ai.tools.sql.AsyncSessionLocal", _test_session_maker()):
        tools = {t.name: t for t in make_sql_tools(str(u1.id))}
        result = await _handler(tools["query_user_data"])({"resource": "portfolios", "limit": 100})

    data = _text(result)
    names = [r["name"] for r in data["rows"]]
    assert "U1 Port" in names
    assert "U2 Port" not in names  # cross-user isolation


@pytest.mark.asyncio
async def test_query_user_data_rejects_unknown_resource(db_session: AsyncSession):
    u = await _make_user(db_session)
    tools = {t.name: t for t in make_sql_tools(str(u.id))}
    result = await _handler(tools["query_user_data"])({"resource": "users", "limit": 10})
    assert "error" in _text(result)


@pytest.mark.asyncio
async def test_query_user_data_caps_limit(db_session: AsyncSession):
    """Requested limit > 100 must be clipped."""
    u = await _make_user(db_session)
    db_session.add_all([Watchlist(user_id=u.id, name=f"W{i}") for i in range(150)])
    await db_session.commit()

    with patch("ai.tools.sql.AsyncSessionLocal", _test_session_maker()):
        tools = {t.name: t for t in make_sql_tools(str(u.id))}
        result = await _handler(tools["query_user_data"])({"resource": "watchlists", "limit": 500})

    assert _text(result)["count"] <= 100


# ── web.web_fetch ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_web_fetch_refuses_non_https():
    tools = {t.name: t for t in make_web_tools()}
    result = await _handler(tools["web_fetch"])({"url": "http://example.com/", "max_chars": 100})
    assert _text(result)["error"] == "Only HTTPS is permitted"


@pytest.mark.asyncio
async def test_web_fetch_refuses_empty_allowlist():
    tools = {t.name: t for t in make_web_tools()}
    # settings.CLAUDE_AGENT_WEBFETCH_ALLOWLIST defaults to empty
    result = await _handler(tools["web_fetch"])({"url": "https://example.com/", "max_chars": 100})
    assert "allowlist" in _text(result)["error"]


@pytest.mark.asyncio
async def test_web_fetch_host_not_on_allowlist(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "CLAUDE_AGENT_WEBFETCH_ALLOWLIST", "good.com")
    tools = {t.name: t for t in make_web_tools()}
    result = await _handler(tools["web_fetch"])({"url": "https://evil.com/", "max_chars": 100})
    assert "not on allowlist" in _text(result)["error"]


# ── python_exec ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_python_exec_basic_math():
    tools = {t.name: t for t in make_python_tools()}
    result = await _handler(tools["python_exec"])({
        "code": "result = sum(range(10))",
        "inputs": {},
    })
    data = _text(result)
    assert data["result"] == 45
    assert data["error"] is None


@pytest.mark.asyncio
async def test_python_exec_uses_inputs():
    tools = {t.name: t for t in make_python_tools()}
    result = await _handler(tools["python_exec"])({
        "code": "result = inputs['a'] * inputs['b']",
        "inputs": {"a": 6, "b": 7},
    })
    assert _text(result)["result"] == 42


@pytest.mark.asyncio
async def test_python_exec_blocks_file_write():
    tools = {t.name: t for t in make_python_tools()}
    result = await _handler(tools["python_exec"])({
        "code": "open('/tmp/should_fail', 'w').write('hi')",
        "inputs": {},
    })
    data = _text(result)
    assert data["error"] is not None  # open() was removed from builtins


@pytest.mark.asyncio
async def test_python_exec_timeout():
    tools = {t.name: t for t in make_python_tools()}
    result = await _handler(tools["python_exec"])({
        "code": "x = 0\nwhile True: x += 1",
        "inputs": {},
    })
    # Either the wall-clock wrapper killed it, or RLIMIT_CPU did
    data = _text(result)
    assert "error" in data or data.get("error") is None and data.get("stdout") == ""
    # Accept either a timeout message or a CPU limit exit; just assert we got SOMETHING back
    assert data != {"result": None, "error": None, "stdout": ""}


# ── financial.get_options_chain ─────────────────────────────────────

def _make_contract(strike: float, expiration: str, ctype: str = "call",
                   volume: int = 0, oi: int = 0) -> dict:
    return {
        "ticker": f"NVDA{expiration.replace('-', '')}{ctype[0].upper()}{int(strike)}",
        "underlying_ticker": "NVDA",
        "contract_type": ctype,
        "expiration_date": expiration,
        "strike_price": strike,
        "last_price": 1.0,
        "volume": volume,
        "open_interest": oi,
        "implied_volatility": 0.4,
        "in_the_money": False,
    }


@pytest.mark.asyncio
async def test_get_options_chain_picks_nearest_expiry():
    tools = {t.name: t for t in make_financial_tools()}
    chain = [
        _make_contract(100, "2026-06-20", volume=10, oi=5),
        _make_contract(100, "2026-09-19", volume=999, oi=999),  # later, ignored
    ]
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = chain
        result = await _handler(tools["get_options_chain"])({"symbol": "NVDA"})

    data = _text(result)
    assert data["expiration"] == "2026-06-20"
    assert data["count"] == 1
    assert data["contracts"][0]["expiration_date"] == "2026-06-20"


@pytest.mark.asyncio
async def test_get_options_chain_caps_at_30_by_liquidity():
    tools = {t.name: t for t in make_financial_tools()}
    chain = [
        _make_contract(strike=k, expiration="2026-06-20",
                       volume=k, oi=k)
        for k in range(1, 51)
    ]
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = chain
        result = await _handler(tools["get_options_chain"])({
            "symbol": "NVDA", "expiration": "2026-06-20",
        })

    data = _text(result)
    assert data["count"] == 30
    # Top liquidity rows survive — strike=50 (highest volume+oi) should be present
    assert any(c["strike_price"] == 50 for c in data["contracts"])
    # Lowest-liquidity strike=1 must be filtered out
    assert all(c["strike_price"] != 1 for c in data["contracts"])


@pytest.mark.asyncio
async def test_get_options_chain_empty_returns_safe_payload():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.us_market_service.get_options",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        result = await _handler(tools["get_options_chain"])({"symbol": "DELISTED"})
    data = _text(result)
    assert data == {"symbol": "DELISTED", "count": 0, "contracts": []}


# ── financial.get_symbol_news ───────────────────────────────────────

@pytest.mark.asyncio
async def test_get_symbol_news_routes_us():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.us_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = [{"title": "AAPL beats earnings", "link": "https://x"}]
        result = await _handler(tools["get_symbol_news"])({
            "symbol": "aapl", "market": "US", "limit": 5,
        })
    mock.assert_awaited_once_with("AAPL", limit=5)
    data = _text(result)
    assert data["market"] == "US"
    assert data["count"] == 1


@pytest.mark.asyncio
async def test_get_symbol_news_routes_tw():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.tw_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = [{"title": "台積電法說", "link": "https://y"}]
        result = await _handler(tools["get_symbol_news"])({
            "symbol": "2330", "market": "tw",
        })
    mock.assert_awaited_once_with("2330", limit=10)
    assert _text(result)["market"] == "TW"


@pytest.mark.asyncio
async def test_get_symbol_news_caps_limit_at_20():
    tools = {t.name: t for t in make_financial_tools()}
    with patch("services.us_market_service.get_news",
               new_callable=AsyncMock) as mock:
        mock.return_value = []
        await _handler(tools["get_symbol_news"])({
            "symbol": "AAPL", "market": "US", "limit": 999,
        })
    # The handler must clip the upstream call to 20.
    mock.assert_awaited_once_with("AAPL", limit=20)


@pytest.mark.asyncio
async def test_get_symbol_news_rejects_unknown_market():
    tools = {t.name: t for t in make_financial_tools()}
    result = await _handler(tools["get_symbol_news"])({
        "symbol": "X", "market": "JP",
    })
    assert "error" in _text(result)


# ── financial.get_symbol_sentiment ──────────────────────────────────

@pytest.mark.asyncio
async def test_get_symbol_sentiment_returns_aggregate():
    # AsyncSessionLocal is auto-patched to the test sessionmaker by conftest's
    # `_override_async_session_local` fixture, so the lazy import inside the
    # handler resolves to the in-memory SQLite engine without explicit setup.
    tools = {t.name: t for t in make_financial_tools()}
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = {
            "symbol": "2330", "considered": 12, "count": 12,
            "avg_score": 0.42, "bullish": 8, "bearish": 1, "neutral": 3,
            "headlines": [],
        }
        result = await _handler(tools["get_symbol_sentiment"])({
            "symbol": "2330", "market": "TW",
        })
    data = _text(result)
    assert data["avg_score"] == 0.42
    assert data["bullish"] == 8


@pytest.mark.asyncio
async def test_get_symbol_sentiment_no_data_returns_zero():
    tools = {t.name: t for t in make_financial_tools()}
    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = None
        result = await _handler(tools["get_symbol_sentiment"])({
            "symbol": "OBSCURE", "market": "US",
        })
    data = _text(result)
    assert data["considered"] == 0
    assert data["headlines"] == []


@pytest.mark.asyncio
async def test_get_symbol_sentiment_rejects_unknown_market():
    tools = {t.name: t for t in make_financial_tools()}
    result = await _handler(tools["get_symbol_sentiment"])({
        "symbol": "X", "market": "JP",
    })
    assert "error" in _text(result)


# ── financial.get_peers ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_peers_us_returns_error():
    tools = {t.name: t for t in make_financial_tools()}
    result = await _handler(tools["get_peers"])({
        "symbol": "AAPL", "market": "US",
    })
    assert "error" in _text(result)


@pytest.mark.asyncio
async def test_get_peers_tw_returns_quote_enriched_rows():
    tools = {t.name: t for t in make_financial_tools()}
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
        result = await _handler(tools["get_peers"])({
            "symbol": "2308", "market": "TW", "limit": 5,
        })

    data = _text(result)
    assert data["industry"] == "半導體業"
    assert data["count"] == 2


@pytest.mark.asyncio
async def test_get_peers_tw_no_industry_returns_empty():
    tools = {t.name: t for t in make_financial_tools()}
    with patch(
        "services.tw_market_service.get_industry_peers", return_value=[],
    ), patch(
        "services.tw_market_service.get_industry", return_value=None,
    ):
        result = await _handler(tools["get_peers"])({
            "symbol": "9999", "market": "TW",
        })
    data = _text(result)
    assert data["peers"] == []


# ── financial.get_financials ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_financials_us_caps_5_periods():
    tools = {t.name: t for t in make_financial_tools()}
    big = [{"period": f"FY{2026 - i}", "revenue": 100 + i} for i in range(12)]
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
        result = await _handler(tools["get_financials"])({
            "symbol": "AAPL", "market": "US",
        })
    data = _text(result)
    assert data["source"] == "yfinance"
    assert len(data["income_statement"]) == 5


@pytest.mark.asyncio
async def test_get_financials_tw_caps_60_rows():
    tools = {t.name: t for t in make_financial_tools()}
    big = [{"date": f"2026-{i:02d}-01", "type": "Revenue", "value": i}
           for i in range(1, 200)]
    with patch(
        "services.tw_market_service.get_financials",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = big
        result = await _handler(tools["get_financials"])({
            "symbol": "2330", "market": "TW",
        })
    data = _text(result)
    assert len(data["rows"]) == 60
    assert data["rows"][-1]["value"] == 199


@pytest.mark.asyncio
async def test_get_financials_rejects_unknown_market():
    tools = {t.name: t for t in make_financial_tools()}
    result = await _handler(tools["get_financials"])({
        "symbol": "X", "market": "JP",
    })
    assert "error" in _text(result)
