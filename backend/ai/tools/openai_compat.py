"""
OpenAI-compatible tool definitions, mirroring the Claude Agent MCP toolset.

Used by providers that consume OpenAI's `tools` / `tool_calls` format
(MiniMax, and future DeepSeek/Qwen/Ollama-with-tool-calling). Calls the
same backend services as `ai/tools/financial.py` and `ai/tools/sql.py`
but without the `claude-agent-sdk` dependency, so it works for any
OpenAI-shaped chat completions endpoint.

Returned shape:
    schemas:  list[dict]                            — JSON for `tools` arg
    dispatch: dict[name, async (args) -> str]       — name → handler

Web-fetch and python-exec tools are intentionally NOT exposed here. They
require sandboxing the Claude Agent SDK provides; surfacing them to
arbitrary OpenAI-compat providers would be a step backward on safety.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.session import AsyncSessionLocal
from models.alert import PriceAlert
from models.portfolio import Holding, Portfolio, Transaction
from models.watchlist import Watchlist, WatchlistItem

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]

_RESOURCES = {
    "portfolios": (Portfolio, ["id", "name", "currency", "created_at"]),
    "holdings": (Holding, ["portfolio_id", "symbol", "market", "quantity", "avg_cost"]),
    "transactions": (
        Transaction,
        ["id", "portfolio_id", "symbol", "market", "tx_type", "quantity", "price", "executed_at"],
    ),
    "watchlists": (Watchlist, ["id", "name", "created_at"]),
    "watchlist_items": (WatchlistItem, ["watchlist_id", "symbol", "market", "added_at"]),
    "alerts": (
        PriceAlert,
        ["id", "symbol", "market", "condition", "target_price", "triggered", "created_at"],
    ),
}


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _row_to_dict(row: Any, cols: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in cols:
        v = getattr(row, c, None)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif hasattr(v, "value"):
            v = v.value
        elif isinstance(v, uuid.UUID):
            v = str(v)
        out[c] = v
    return out


def build_openai_compat_toolset(
    user_id: str,
) -> tuple[list[dict], dict[str, ToolHandler]]:
    """Compose per-user OpenAI-compat tool schemas + dispatch table.

    `user_id` is closed over so query_user_data can never see another user's
    rows even if the model fabricates a user_id field in its arguments.
    """
    uid = uuid.UUID(user_id)

    async def get_quote(args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol", "")).upper()
        market = str(args.get("market", "")).upper()
        try:
            if market == "US":
                from services.us_market_service import get_quote as _svc
            elif market == "TW":
                from services.tw_market_service import get_quote as _svc
            else:
                return _dump({"error": f"Unsupported market: {market}"})
            return _dump(await _svc(symbol))
        except Exception as exc:
            logger.warning("openai_compat.get_quote failed %s %s: %s", market, symbol, exc)
            return _dump({"error": str(exc)})

    async def run_dcf(args: dict[str, Any]) -> str:
        from services.analytics_service import run_dcf_analysis
        try:
            return _dump(await run_dcf_analysis(
                symbol=args["symbol"],
                market=args["market"],
                overrides=args.get("overrides") or {},
            ))
        except Exception as exc:
            logger.warning("openai_compat.run_dcf failed: %s", exc)
            return _dump({"error": str(exc)})

    async def run_var(args: dict[str, Any]) -> str:
        from services.analytics_service import run_var_analysis
        try:
            return _dump(await run_var_analysis(
                symbols=args["symbols"],
                markets=args["markets"],
                weights=args["weights"],
                portfolio_value=float(args["portfolio_value"]),
                method=args.get("method", "historical"),
                confidence=float(args.get("confidence", 0.95)),
                horizon_days=int(args.get("horizon_days", 1)),
            ))
        except Exception as exc:
            logger.warning("openai_compat.run_var failed: %s", exc)
            return _dump({"error": str(exc)})

    async def run_backtest(args: dict[str, Any]) -> str:
        from services.analytics_service import run_backtest_analysis
        try:
            result = await run_backtest_analysis(
                symbols=args["symbols"],
                markets=args["markets"],
                strategy=args["strategy"],
                params=args.get("params") or {},
                start_date=args["start_date"],
                end_date=args["end_date"],
                initial_capital=float(args.get("initial_capital", 100_000)),
            )
            if "equity_curve" in result:
                ec = result["equity_curve"]
                if isinstance(ec, list) and len(ec) > 20:
                    result = {**result, "equity_curve": {
                        "count": len(ec), "head": ec[:10], "tail": ec[-10:],
                    }}
            return _dump(result)
        except Exception as exc:
            logger.warning("openai_compat.run_backtest failed: %s", exc)
            return _dump({"error": str(exc)})

    async def query_user_data(args: dict[str, Any]) -> str:
        resource = str(args.get("resource", "")).lower()
        limit = min(int(args.get("limit", 50)), 100)
        if resource not in _RESOURCES:
            return _dump({"error": f"Unknown resource. Allowed: {sorted(_RESOURCES.keys())}"})

        model, cols = _RESOURCES[resource]
        async with AsyncSessionLocal() as session:
            try:
                stmt = select(model).limit(limit)
                if hasattr(model, "user_id"):
                    stmt = stmt.where(model.user_id == uid)
                elif model is Holding or model is Transaction:
                    stmt = stmt.join(Portfolio).where(Portfolio.user_id == uid)
                elif model is WatchlistItem:
                    stmt = stmt.join(Watchlist).where(Watchlist.user_id == uid)
                else:
                    return _dump({"error": "Resource cannot be user-scoped safely"})

                if model is Watchlist:
                    stmt = stmt.options(selectinload(Watchlist.items))

                rows = (await session.execute(stmt)).scalars().all()
                return _dump({
                    "resource": resource,
                    "count": len(rows),
                    "rows": [_row_to_dict(r, cols) for r in rows],
                })
            except Exception as exc:
                logger.warning("openai_compat.query_user_data failed: %s", exc)
                return _dump({"error": str(exc)})

    async def get_options_chain(args: dict[str, Any]) -> str:
        # The underlying yfinance / Polygon chain can run to thousands of
        # rows across all expiries. Without expiration the tool focuses on
        # the nearest one and caps at the 30 most-liquid contracts (volume
        # + open_interest) so the LLM gets a strike grid it can reason
        # about instead of dumping its context window.
        from services.us_market_service import get_options
        symbol = str(args.get("symbol", "")).upper()
        expiration = args.get("expiration")
        try:
            chain = await get_options(symbol, expiration)
            if not chain:
                return _dump({"symbol": symbol, "count": 0, "contracts": []})
            if not expiration:
                expiries = sorted({
                    c.get("expiration_date") for c in chain if c.get("expiration_date")
                })
                if expiries:
                    expiration = expiries[0]
                    chain = [c for c in chain if c.get("expiration_date") == expiration]
            chain.sort(
                key=lambda c: (c.get("volume", 0) or 0) + (c.get("open_interest", 0) or 0),
                reverse=True,
            )
            chain = chain[:30]
            return _dump({
                "symbol": symbol,
                "expiration": expiration,
                "count": len(chain),
                "contracts": chain,
            })
        except Exception as exc:
            logger.warning("openai_compat.get_options_chain failed %s: %s", symbol, exc)
            return _dump({"error": str(exc)})

    async def get_symbol_news(args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol", "")).upper()
        market = str(args.get("market", "")).upper()
        limit = max(1, min(int(args.get("limit", 10)), 20))
        try:
            if market == "US":
                from services.us_market_service import get_news as _svc
            elif market == "TW":
                from services.tw_market_service import get_news as _svc
            else:
                return _dump({"error": f"Unsupported market: {market}"})
            items = await _svc(symbol, limit=limit)
            return _dump({
                "symbol": symbol,
                "market": market,
                "count": len(items),
                "items": items,
            })
        except Exception as exc:
            logger.warning(
                "openai_compat.get_symbol_news failed %s %s: %s", market, symbol, exc,
            )
            return _dump({"error": str(exc)})

    async def get_symbol_sentiment(args: dict[str, Any]) -> str:
        from services.news_sentiment_service import read_symbol_sentiment
        symbol = str(args.get("symbol", "")).upper()
        market = str(args.get("market", "")).upper()
        max_age = max(1, min(int(args.get("max_age_hours", 168)), 720))
        if market not in ("US", "TW", "GLOBAL"):
            return _dump({"error": f"Unsupported market: {market}"})
        async with AsyncSessionLocal() as session:
            try:
                agg = await read_symbol_sentiment(
                    session, market=market, symbol=symbol, max_age_hours=max_age,
                )
                if agg is None:
                    return _dump({
                        "symbol": symbol,
                        "market": market,
                        "max_age_hours": max_age,
                        "considered": 0,
                        "headlines": [],
                    })
                return _dump(agg)
            except Exception as exc:
                logger.warning(
                    "openai_compat.get_symbol_sentiment failed %s %s: %s",
                    market, symbol, exc,
                )
                return _dump({"error": str(exc)})

    async def get_peers(args: dict[str, Any]) -> str:
        # TW-only: industry peers come from `_industry_map` populated by
        # the daily symbol cron. US has no curated industry mapping in
        # this codebase (yfinance.info is too slow per-symbol), so the
        # tool returns an explicit error rather than a stale fallback —
        # personas can fall back to `get_financials` + per-symbol
        # comparison when the peer table isn't available.
        symbol = str(args.get("symbol", "")).upper()
        market = str(args.get("market", "")).upper()
        limit = max(1, min(int(args.get("limit", 5)), 10))
        if market != "TW":
            return _dump({
                "error": "Industry peers currently supported for TW only. "
                         "For US, fetch get_financials per symbol and compare manually.",
            })
        from services.tw_market_service import (
            get_company_name, get_industry, get_industry_peers, get_quote,
        )
        try:
            peer_syms = get_industry_peers(symbol)[:limit]
        except Exception as exc:
            logger.warning("openai_compat.get_peers lookup failed %s: %s", symbol, exc)
            return _dump({"error": str(exc)})
        if not peer_syms:
            return _dump({
                "symbol": symbol,
                "industry": get_industry(symbol),
                "peers": [],
            })
        rows: list[dict[str, Any]] = []
        for sym in peer_syms:
            try:
                q = await get_quote(sym)
            except Exception as exc:
                logger.warning(
                    "openai_compat.get_peers quote failed %s: %s", sym, exc,
                )
                q = {}
            rows.append({
                "symbol": sym,
                "name_zh": q.get("name_zh") or get_company_name(sym),
                "price": q.get("price"),
                "change_pct": q.get("change_pct"),
                "pe_ratio": q.get("pe_ratio"),
                "pb_ratio": q.get("pb_ratio"),
                "dividend_yield": q.get("dividend_yield"),
            })
        return _dump({
            "symbol": symbol,
            "industry": get_industry(symbol),
            "count": len(rows),
            "peers": rows,
        })

    async def get_financials(args: dict[str, Any]) -> str:
        # Cap each statement at the most recent 5 periods so the LLM gets
        # an annual / quarterly trend window without dumping the entire
        # multi-year archive into its context. TW returns a flat row list
        # ({date, type, value}); we keep the latest 60 rows so a 5-quarter
        # × ~12-metric pivot has full coverage.
        symbol = str(args.get("symbol", "")).upper()
        market = str(args.get("market", "")).upper()
        try:
            if market == "US":
                from services.us_market_service import get_financials as _svc
                data = await _svc(symbol)
                # Dict with income_statement / balance_sheet / cash_flow.
                # Polygon's `data` payload skips this shape (it has its own
                # nested form); keep the raw output but cap each list.
                if isinstance(data, dict):
                    out: dict[str, Any] = {
                        "symbol": symbol,
                        "market": market,
                        "source": data.get("source"),
                    }
                    for k in ("income_statement", "balance_sheet", "cash_flow"):
                        rows = data.get(k)
                        if isinstance(rows, list):
                            out[k] = rows[:5]
                    if "data" in data and "income_statement" not in out:
                        # Polygon nested shape
                        out["data"] = data["data"]
                    return _dump(out)
                return _dump({"symbol": symbol, "market": market, "raw": data})
            elif market == "TW":
                from services.tw_market_service import get_financials as _svc
                rows = await _svc(symbol)
                if not isinstance(rows, list):
                    rows = []
                return _dump({
                    "symbol": symbol,
                    "market": market,
                    "count": len(rows),
                    "rows": rows[-60:],
                })
            else:
                return _dump({"error": f"Unsupported market: {market}"})
        except Exception as exc:
            logger.warning(
                "openai_compat.get_financials failed %s %s: %s",
                market, symbol, exc,
            )
            return _dump({"error": str(exc)})

    async def get_institutional_history(args: dict[str, Any]) -> str:
        # 法人買賣超 daily series. TW only — the underlying service is
        # backed by the daily TWSE ingest archive (`tw_institutional_daily`)
        # so a 90-day query is one indexed range scan, no per-day fan-out.
        symbol = str(args.get("symbol", "")).upper()
        days = max(1, min(int(args.get("days", 30)), 90))
        try:
            from services.tw_market_service import get_institutional
            rows = await get_institutional(symbol, days=days)
            return _dump({
                "symbol": symbol,
                "days": days,
                "count": len(rows),
                "rows": rows,
            })
        except Exception as exc:
            logger.warning(
                "openai_compat.get_institutional_history failed %s: %s",
                symbol, exc,
            )
            return _dump({"error": str(exc)})

    async def get_margin_history(args: dict[str, Any]) -> str:
        # 融資融券 daily series. Same TW-only DB-archive read tier as
        # get_institutional_history.
        symbol = str(args.get("symbol", "")).upper()
        days = max(1, min(int(args.get("days", 30)), 90))
        try:
            from services.tw_market_service import get_margin
            rows = await get_margin(symbol, days=days)
            return _dump({
                "symbol": symbol,
                "days": days,
                "count": len(rows),
                "rows": rows,
            })
        except Exception as exc:
            logger.warning(
                "openai_compat.get_margin_history failed %s: %s",
                symbol, exc,
            )
            return _dump({"error": str(exc)})

    async def get_top_brokers(args: dict[str, Any]) -> str:
        # 主力分點: top buyers + top sellers (5-day net buy by broker)
        # for one TW symbol. Capped at top_n=10 to keep response size
        # bounded; default top_n=3 matches the discussion ctx block.
        symbol = str(args.get("symbol", "")).upper()
        days = max(1, min(int(args.get("days", 5)), 30))
        top_n = max(1, min(int(args.get("top_n", 3)), 10))
        try:
            from services.broker_concentration_service import (
                get_top_brokers_for_symbol,
            )
            payload = await get_top_brokers_for_symbol(
                symbol, days=days, top_n=top_n,
            )
            if payload is None:
                return _dump({
                    "symbol": symbol,
                    "days": days,
                    "top_buyers": [],
                    "top_sellers": [],
                    "note": "no broker data in window (FinMind quota / paywall / "
                            "thinly-traded symbol)",
                })
            return _dump(payload)
        except Exception as exc:
            logger.warning(
                "openai_compat.get_top_brokers failed %s: %s", symbol, exc,
            )
            return _dump({"error": str(exc)})

    async def get_taifex_positioning(args: dict[str, Any]) -> str:
        # Index-futures three-investor net OI snapshot + 5-day delta.
        # Default contract TX (大盤). Returns None when FinMind quota
        # is exhausted; we surface that as a structured note instead
        # of an error so the LLM treats it as "data unavailable" rather
        # than retrying.
        contract = str(args.get("contract", "TX")).upper()
        try:
            from services.derivatives_service import get_taifex_positioning as _svc
            payload = await _svc(contract=contract)
            if payload is None:
                return _dump({
                    "contract": contract,
                    "note": "no positioning data (FinMind quota / paywall / "
                            "empty window)",
                })
            return _dump(payload)
        except Exception as exc:
            logger.warning(
                "openai_compat.get_taifex_positioning failed %s: %s",
                contract, exc,
            )
            return _dump({"error": str(exc)})

    schemas: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "get_quote",
                "description": "Fetch the latest quote for a stock symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker, e.g. NVDA, 2330"},
                        "market": {"type": "string", "enum": ["US", "TW"]},
                    },
                    "required": ["symbol", "market"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_dcf",
                "description": (
                    "Run a 2-stage DCF valuation. Returns intrinsic value per share, "
                    "5x3 sensitivity grid (WACC × growth), and bull/base/bear scenarios."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "market": {"type": "string", "enum": ["US", "TW"]},
                        "overrides": {
                            "type": "object",
                            "description": "Optional: fcf_history, growth_rate_1, wacc, shares, net_debt, current_price.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["symbol", "market"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_var",
                "description": (
                    "Compute Value-at-Risk for a portfolio. method: "
                    "historical | parametric | monte_carlo | all."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "markets": {"type": "array", "items": {"type": "string"}},
                        "weights": {"type": "array", "items": {"type": "number"}},
                        "portfolio_value": {"type": "number"},
                        "method": {
                            "type": "string",
                            "enum": ["historical", "parametric", "monte_carlo", "all"],
                        },
                        "confidence": {"type": "number", "default": 0.95},
                        "horizon_days": {"type": "integer", "default": 1},
                    },
                    "required": ["symbols", "markets", "weights", "portfolio_value"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_backtest",
                "description": (
                    "Backtest a strategy over a date range. "
                    "strategy: 'sma_crossover' or 'rsi_mean_reversion'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "markets": {"type": "array", "items": {"type": "string"}},
                        "strategy": {
                            "type": "string",
                            "enum": ["sma_crossover", "rsi_mean_reversion"],
                        },
                        "params": {"type": "object", "additionalProperties": True},
                        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "initial_capital": {"type": "number", "default": 100000},
                    },
                    "required": ["symbols", "markets", "strategy", "start_date", "end_date"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_user_data",
                "description": (
                    "Read-only access to the caller's own data. "
                    "Always scoped to the calling user — cannot query other users."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "resource": {
                            "type": "string",
                            "enum": sorted(_RESOURCES.keys()),
                        },
                        "limit": {"type": "integer", "default": 50, "maximum": 100},
                    },
                    "required": ["resource"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_options_chain",
                "description": (
                    "US options chain for a ticker — top 30 most-liquid contracts "
                    "(by volume + open_interest) on the nearest expiration by "
                    "default. Pass `expiration` (YYYY-MM-DD) to target a specific "
                    "date. Each row includes strike, bid/ask, last_price, "
                    "implied_volatility, open_interest, volume."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "US ticker, e.g. NVDA"},
                        "expiration": {
                            "type": "string",
                            "description": "Optional YYYY-MM-DD. Omit for nearest expiry.",
                        },
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_symbol_news",
                "description": (
                    "Recent news headlines for a specific symbol. Backed by the "
                    "Google News RSS pipeline (US: en-US; TW: zh-TW) plus the "
                    "FinMind ingest archive on TW. Returns up to 20 items."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "market": {"type": "string", "enum": ["US", "TW"]},
                        "limit": {"type": "integer", "default": 10, "maximum": 20},
                    },
                    "required": ["symbol", "market"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_symbol_sentiment",
                "description": (
                    "Aggregate sentiment-scored news for one symbol over a recent "
                    "window. Returns avg_score in [-1, +1], bullish/bearish/neutral "
                    "counts, and the most recent headlines with their scores. "
                    "Returns count=0 + empty headlines when no scored articles "
                    "exist (e.g. fresh ticker, narrow query)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "market": {"type": "string", "enum": ["US", "TW", "GLOBAL"]},
                        "max_age_hours": {
                            "type": "integer", "default": 168, "maximum": 720,
                            "description": "Look-back window in hours. Default 168 (7 days).",
                        },
                    },
                    "required": ["symbol", "market"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_peers",
                "description": (
                    "Industry peers for a symbol (TW only). Returns up to 10 peers "
                    "in the same 產業別 with their latest quote + PE/PB/dividend "
                    "yield. Useful for value-investing comparison ('is 2330's "
                    "PE > or < same-industry median?'). US is not yet supported "
                    "(no curated industry mapping); fall back to get_financials "
                    "per-symbol for US comparison."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "market": {"type": "string", "enum": ["US", "TW"]},
                        "limit": {"type": "integer", "default": 5, "maximum": 10},
                    },
                    "required": ["symbol", "market"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_financials",
                "description": (
                    "Three financial statements (income / balance / cash flow) "
                    "for a symbol. US returns per-statement annual rows (capped "
                    "at the most recent 5 periods). TW returns a flat FinMind row "
                    "list (date / type / value) capped at the most recent 60 rows "
                    "(~5 quarters × ~12 metrics). Use for ROE / margin / "
                    "leverage / FCF analysis."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "market": {"type": "string", "enum": ["US", "TW"]},
                    },
                    "required": ["symbol", "market"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_institutional_history",
                "description": (
                    "TW only — daily 法人買賣超 (foreign / SITC / dealer "
                    "buy + sell volumes) for a symbol over the requested "
                    "lookback window. Backed by the daily TWSE ingest "
                    "archive — a 90-day query is one indexed scan, no "
                    "FinMind quota burn."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "TW ticker, e.g. 2330"},
                        "days": {"type": "integer", "default": 30, "maximum": 90},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_margin_history",
                "description": (
                    "TW only — daily 融資融券 (margin purchase / balance + "
                    "short sale / balance) for a symbol. Same DB-archive "
                    "read tier as get_institutional_history."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "TW ticker"},
                        "days": {"type": "integer", "default": 30, "maximum": 90},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_top_brokers",
                "description": (
                    "TW only — 主力分點: top buyers + top sellers ranked by "
                    "N-day net buy volume per broker for one symbol. Useful "
                    "for spotting accumulation / distribution by specific "
                    "broker desks. Live FinMind, 24h Redis cache."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "TW ticker"},
                        "days": {"type": "integer", "default": 5, "maximum": 30,
                                 "description": "Net-buy aggregation window in trading days."},
                        "top_n": {"type": "integer", "default": 3, "maximum": 10,
                                  "description": "Rows on each side (top buyers + top sellers)."},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_taifex_positioning",
                "description": (
                    "TW only — index-futures three-investor (foreign / SITC / "
                    "dealer) net open-interest snapshot + 5-day change for the "
                    "specified contract. Default contract is TX (TAIEX). "
                    "Returns trend = bullish | bearish | neutral. Foreign-net OI "
                    "leads spot moves by 1-2 trading days historically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contract": {
                            "type": "string", "default": "TX",
                            "description": "TAIFEX contract code (TX / MTX / TE / TF). "
                                           "TX = TAIEX, MTX = mini TAIEX, TE = electronics, "
                                           "TF = financials.",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]

    dispatch: dict[str, ToolHandler] = {
        "get_quote": get_quote,
        "run_dcf": run_dcf,
        "run_var": run_var,
        "run_backtest": run_backtest,
        "query_user_data": query_user_data,
        "get_options_chain": get_options_chain,
        "get_symbol_news": get_symbol_news,
        "get_symbol_sentiment": get_symbol_sentiment,
        "get_peers": get_peers,
        "get_financials": get_financials,
        "get_institutional_history": get_institutional_history,
        "get_margin_history": get_margin_history,
        "get_top_brokers": get_top_brokers,
        "get_taifex_positioning": get_taifex_positioning,
    }
    return schemas, dispatch
