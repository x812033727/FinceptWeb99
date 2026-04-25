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
    ]

    dispatch: dict[str, ToolHandler] = {
        "get_quote": get_quote,
        "run_dcf": run_dcf,
        "run_var": run_var,
        "run_backtest": run_backtest,
        "query_user_data": query_user_data,
    }
    return schemas, dispatch
