"""
Financial analysis tools (market data + analytics). Wrap existing services.

Each tool returns MCP-format content; we compact large arrays before
returning so a 252-bar history doesn't blow up the model's context.
"""
import json
import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)


def _text(payload: Any) -> dict:
    """Wrap a Python value as an MCP text content block."""
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": payload}]}


def _compact_bars(bars: list[dict], limit: int = 10) -> dict:
    """Return a summary + head/tail instead of the full price series."""
    if not bars:
        return {"count": 0, "head": [], "tail": []}
    return {
        "count": len(bars),
        "head": bars[:limit],
        "tail": bars[-limit:] if len(bars) > limit else [],
    }


def make_financial_tools() -> list[SdkMcpTool]:
    """Build the financial tool list. No user scoping needed (market data is public)."""

    @tool(
        "get_quote",
        "Fetch the latest quote for a stock symbol. market='US' or 'TW'. "
        "Returns price, change_pct, volume, market_cap.",
        {"symbol": str, "market": str},
    )
    async def get_quote(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        market = args["market"].upper()
        try:
            if market == "US":
                from services.us_market_service import get_quote as _svc
            elif market == "TW":
                from services.tw_market_service import get_quote as _svc
            else:
                return _text({"error": f"Unsupported market: {market}"})
            q = await _svc(symbol)
            return _text(q)
        except Exception as exc:
            logger.warning("get_quote tool failed: %s %s: %s", market, symbol, exc)
            return _text({"error": str(exc)})

    @tool(
        "run_dcf",
        "Run a 2-stage DCF valuation. Provide symbol, market, and optional overrides "
        "(fcf_history, growth_rate_1, wacc, shares, net_debt, current_price).",
        {"symbol": str, "market": str, "overrides": dict},
    )
    async def run_dcf(args: dict[str, Any]) -> dict:
        from services.analytics_service import run_dcf_analysis
        try:
            result = await run_dcf_analysis(
                symbol=args["symbol"],
                market=args["market"],
                overrides=args.get("overrides") or {},
            )
            # Sensitivity grid is 5x3; scenarios are bull/base/bear. Keep full.
            return _text(result)
        except Exception as exc:
            logger.warning("run_dcf tool failed: %s", exc)
            return _text({"error": str(exc)})

    @tool(
        "run_var",
        "Compute Value-at-Risk for a portfolio. method is 'historical' | 'parametric' | "
        "'monte_carlo' | 'all'. symbols/markets/weights are parallel arrays.",
        {
            "symbols": list,
            "markets": list,
            "weights": list,
            "portfolio_value": float,
            "method": str,
            "confidence": float,
            "horizon_days": int,
        },
    )
    async def run_var(args: dict[str, Any]) -> dict:
        from services.analytics_service import run_var_analysis
        try:
            result = await run_var_analysis(
                symbols=args["symbols"],
                markets=args["markets"],
                weights=args["weights"],
                portfolio_value=float(args["portfolio_value"]),
                method=args.get("method", "historical"),
                confidence=float(args.get("confidence", 0.95)),
                horizon_days=int(args.get("horizon_days", 1)),
            )
            return _text(result)
        except Exception as exc:
            logger.warning("run_var tool failed: %s", exc)
            return _text({"error": str(exc)})

    @tool(
        "run_backtest",
        "Backtest a strategy ('sma_crossover' or 'rsi_mean_reversion') over a date range. "
        "Returns metrics (sharpe, max_drawdown, total_return) and a compacted equity curve.",
        {
            "symbols": list,
            "markets": list,
            "strategy": str,
            "params": dict,
            "start_date": str,
            "end_date": str,
            "initial_capital": float,
        },
    )
    async def run_backtest(args: dict[str, Any]) -> dict:
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
                result = {**result, "equity_curve": _compact_bars(result["equity_curve"])}
            return _text(result)
        except Exception as exc:
            logger.warning("run_backtest tool failed: %s", exc)
            return _text({"error": str(exc)})

    return [get_quote, run_dcf, run_var, run_backtest]
