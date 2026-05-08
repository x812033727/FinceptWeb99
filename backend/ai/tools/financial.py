"""
Financial analysis tools (market data + analytics). Wrap existing services.

Each tool returns MCP-format content; we compact large arrays before
returning so a 252-bar history doesn't blow up the model's context.
"""
import json
import logging
from datetime import date
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


def make_financial_tools(
    *, as_of_date: date | None = None,
) -> list[SdkMcpTool]:
    """Build the financial tool list. No user scoping needed (market data is public).

    `as_of_date` (backtest mode): closure-injected into the 4 TW
    chip-flow tools so a backtest discussion's tool calls anchor at
    the historical date instead of `today`. The LLM doesn't see
    `as_of` as a tool param — backtest correctness can't be defeated
    by the persona forgetting to pass it.
    """

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
            kwargs: dict[str, Any] = {}
            if as_of_date is not None:
                kwargs["as_of"] = as_of_date
            q = await _svc(symbol, **kwargs)
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

    @tool(
        "get_options_chain",
        "US options chain — top 30 most-liquid contracts (volume + open_interest) "
        "on the nearest expiration by default. Pass expiration='YYYY-MM-DD' for a "
        "specific date. Each row: strike_price, bid, ask, last_price, "
        "implied_volatility, open_interest, volume, contract_type.",
        {"symbol": str, "expiration": str},
    )
    async def get_options_chain(args: dict[str, Any]) -> dict:
        from services.us_market_service import get_options
        symbol = args["symbol"].upper()
        expiration = args.get("expiration") or None
        try:
            chain = await get_options(symbol, expiration)
            if not chain:
                return _text({"symbol": symbol, "count": 0, "contracts": []})
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
            return _text({
                "symbol": symbol,
                "expiration": expiration,
                "count": len(chain),
                "contracts": chain,
            })
        except Exception as exc:
            logger.warning("get_options_chain tool failed: %s %s", symbol, exc)
            return _text({"error": str(exc)})

    @tool(
        "get_symbol_news",
        "Recent news headlines for one symbol. Google News RSS-backed "
        "(US: en-US; TW: zh-TW + FinMind archive). Returns up to 20 items.",
        {"symbol": str, "market": str, "limit": int},
    )
    async def get_symbol_news(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        market = args["market"].upper()
        limit = max(1, min(int(args.get("limit", 10)), 20))
        try:
            if market == "US":
                from services.us_market_service import get_news as _svc
            elif market == "TW":
                from services.tw_market_service import get_news as _svc
            else:
                return _text({"error": f"Unsupported market: {market}"})
            items = await _svc(symbol, limit=limit)
            return _text({
                "symbol": symbol,
                "market": market,
                "count": len(items),
                "items": items,
            })
        except Exception as exc:
            logger.warning("get_symbol_news tool failed: %s %s %s", market, symbol, exc)
            return _text({"error": str(exc)})

    @tool(
        "get_symbol_sentiment",
        "Aggregate sentiment-scored news for one symbol over a recent window. "
        "Returns avg_score in [-1, +1], bullish/bearish/neutral counts, and top "
        "headlines. Returns count=0 when no scored articles exist for the symbol.",
        {"symbol": str, "market": str, "max_age_hours": int},
    )
    async def get_symbol_sentiment(args: dict[str, Any]) -> dict:
        from db.session import AsyncSessionLocal
        from services.news_sentiment_service import read_symbol_sentiment
        symbol = args["symbol"].upper()
        market = args["market"].upper()
        max_age = max(1, min(int(args.get("max_age_hours", 168)), 720))
        if market not in ("US", "TW", "GLOBAL"):
            return _text({"error": f"Unsupported market: {market}"})
        async with AsyncSessionLocal() as session:
            try:
                agg = await read_symbol_sentiment(
                    session, market=market, symbol=symbol, max_age_hours=max_age,
                )
                if agg is None:
                    return _text({
                        "symbol": symbol,
                        "market": market,
                        "max_age_hours": max_age,
                        "considered": 0,
                        "headlines": [],
                    })
                return _text(agg)
            except Exception as exc:
                logger.warning("get_symbol_sentiment tool failed: %s %s %s",
                               market, symbol, exc)
                return _text({"error": str(exc)})

    @tool(
        "get_peers",
        "Industry peers for a TW symbol — up to 10 same-industry peers with "
        "latest quote + PE/PB/dividend yield. US is not yet supported (no "
        "curated industry mapping); fall back to get_financials per-symbol.",
        {"symbol": str, "market": str, "limit": int},
    )
    async def get_peers(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        market = args["market"].upper()
        limit = max(1, min(int(args.get("limit", 5)), 10))
        if market != "TW":
            return _text({
                "error": "Industry peers currently supported for TW only.",
            })
        from services.tw_market_service import (
            get_company_name, get_industry, get_industry_peers, get_quote,
        )
        try:
            peer_syms = get_industry_peers(symbol)[:limit]
        except Exception as exc:
            logger.warning("get_peers tool lookup failed: %s %s", symbol, exc)
            return _text({"error": str(exc)})
        if not peer_syms:
            return _text({
                "symbol": symbol,
                "industry": get_industry(symbol),
                "peers": [],
            })
        rows = []
        for sym in peer_syms:
            try:
                q = await get_quote(sym)
            except Exception as exc:
                logger.warning("get_peers tool quote failed: %s %s", sym, exc)
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
        return _text({
            "symbol": symbol,
            "industry": get_industry(symbol),
            "count": len(rows),
            "peers": rows,
        })

    @tool(
        "get_financials",
        "Income / balance / cash-flow statements for a symbol. US: annual "
        "rows per statement (top 5 periods). TW: flat FinMind rows ({date, "
        "type, value}) capped at the most recent 60 rows.",
        {"symbol": str, "market": str},
    )
    async def get_financials(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        market = args["market"].upper()
        try:
            if market == "US":
                from services.us_market_service import get_financials as _svc
                data = await _svc(symbol)
                if isinstance(data, dict):
                    out: dict[str, Any] = {
                        "symbol": symbol, "market": market,
                        "source": data.get("source"),
                    }
                    for k in ("income_statement", "balance_sheet", "cash_flow"):
                        rows = data.get(k)
                        if isinstance(rows, list):
                            out[k] = rows[:5]
                    if "data" in data and "income_statement" not in out:
                        out["data"] = data["data"]
                    return _text(out)
                return _text({"symbol": symbol, "market": market, "raw": data})
            elif market == "TW":
                from services.tw_market_service import get_financials as _svc
                tw_kwargs: dict[str, Any] = {}
                if as_of_date is not None:
                    tw_kwargs["as_of"] = as_of_date
                rows = await _svc(symbol, **tw_kwargs)
                if not isinstance(rows, list):
                    rows = []
                return _text({
                    "symbol": symbol, "market": market,
                    "as_of": as_of_date.isoformat() if as_of_date else None,
                    "count": len(rows),
                    "rows": rows[-60:],
                })
            else:
                return _text({"error": f"Unsupported market: {market}"})
        except Exception as exc:
            logger.warning("get_financials tool failed: %s %s %s",
                           market, symbol, exc)
            return _text({"error": str(exc)})

    @tool(
        "get_institutional_history",
        "TW only — daily 法人買賣超 (foreign / SITC / dealer buy + sell volumes) "
        "for a symbol. Backed by the daily TWSE ingest archive — a 90-day "
        "query is one indexed scan. Default days=30, max=90.",
        {"symbol": str, "days": int},
    )
    async def get_institutional_history(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        days = max(1, min(int(args.get("days", 30)), 90))
        try:
            from services.tw_market_service import get_institutional
            rows = await get_institutional(symbol, days=days, as_of=as_of_date)
            return _text({
                "symbol": symbol, "days": days,
                "as_of": as_of_date.isoformat() if as_of_date else None,
                "count": len(rows), "rows": rows,
            })
        except Exception as exc:
            logger.warning(
                "get_institutional_history tool failed: %s %s", symbol, exc,
            )
            return _text({"error": str(exc)})

    @tool(
        "get_margin_history",
        "TW only — daily 融資融券 (margin purchase / balance + short sale / "
        "balance). Same DB-archive read tier as get_institutional_history.",
        {"symbol": str, "days": int},
    )
    async def get_margin_history(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        days = max(1, min(int(args.get("days", 30)), 90))
        try:
            from services.tw_market_service import get_margin
            rows = await get_margin(symbol, days=days, as_of=as_of_date)
            return _text({
                "symbol": symbol, "days": days,
                "as_of": as_of_date.isoformat() if as_of_date else None,
                "count": len(rows), "rows": rows,
            })
        except Exception as exc:
            logger.warning("get_margin_history tool failed: %s %s", symbol, exc)
            return _text({"error": str(exc)})

    @tool(
        "get_top_brokers",
        "TW only — 主力分點: top buyers + top sellers by N-day net buy volume "
        "per broker for one symbol. Useful for accumulation / distribution "
        "spotting. Live FinMind + 24h cache.",
        {"symbol": str, "days": int, "top_n": int},
    )
    async def get_top_brokers(args: dict[str, Any]) -> dict:
        symbol = args["symbol"].upper()
        days = max(1, min(int(args.get("days", 5)), 30))
        top_n = max(1, min(int(args.get("top_n", 3)), 10))
        try:
            from services.broker_concentration_service import (
                get_top_brokers_for_symbol,
            )
            payload = await get_top_brokers_for_symbol(
                symbol, as_of=as_of_date, days=days, top_n=top_n,
            )
            if payload is None:
                return _text({
                    "symbol": symbol, "days": days,
                    "as_of": as_of_date.isoformat() if as_of_date else None,
                    "top_buyers": [], "top_sellers": [],
                    "note": "no broker data in window",
                })
            return _text(payload)
        except Exception as exc:
            logger.warning("get_top_brokers tool failed: %s %s", symbol, exc)
            return _text({"error": str(exc)})

    @tool(
        "compare_quotes",
        "Fetch quotes for up to 10 symbols in parallel — returns one row per "
        "ticker side-by-side. Use this instead of N sequential get_quote "
        "calls when comparing names; saves max_turns budget. Per-symbol "
        "failures isolated into an `errors` list.",
        {"symbols": list, "market": str},
    )
    async def compare_quotes(args: dict[str, Any]) -> dict:
        import asyncio
        raw_syms = args.get("symbols") or []
        if not isinstance(raw_syms, list):
            return _text({"error": "symbols must be a list"})
        symbols = [str(s).upper() for s in raw_syms if s][:10]
        market = str(args.get("market", "")).upper()
        if not symbols:
            return _text({"error": "symbols list is empty"})
        if market == "US":
            from services.us_market_service import get_quote as _svc
        elif market == "TW":
            from services.tw_market_service import get_quote as _svc
        else:
            return _text({"error": f"Unsupported market: {market}"})
        kwargs: dict[str, Any] = {}
        if as_of_date is not None:
            kwargs["as_of"] = as_of_date

        async def _one(sym: str) -> tuple[str, dict | None, str | None]:
            try:
                return sym, await _svc(sym, **kwargs), None
            except Exception as exc:
                return sym, None, str(exc)

        results = await asyncio.gather(*(_one(s) for s in symbols))
        quotes: list[dict] = []
        errors: list[dict] = []
        for sym, q, err in results:
            if err is not None:
                errors.append({"symbol": sym, "error": err})
            elif not q:
                errors.append({"symbol": sym, "error": "no data"})
            else:
                quotes.append(q)
        return _text({
            "market": market,
            "as_of": as_of_date.isoformat() if as_of_date else None,
            "count": len(quotes),
            "quotes": quotes,
            "errors": errors,
        })

    @tool(
        "get_taifex_positioning",
        "TW only — index-futures three-investor (foreign / SITC / dealer) "
        "net open-interest snapshot + 5-day change for the contract. "
        "Returns trend = bullish | bearish | neutral. Foreign-net OI leads "
        "spot moves 1-2 trading days historically. Default contract: TX "
        "(TAIEX); other: MTX, TE, TF.",
        {"contract": str},
    )
    async def get_taifex_positioning(args: dict[str, Any]) -> dict:
        contract = str(args.get("contract", "TX")).upper()
        try:
            from services.derivatives_service import (
                get_taifex_positioning as _svc,
            )
            payload = await _svc(contract=contract, as_of=as_of_date)
            if payload is None:
                return _text({
                    "contract": contract,
                    "as_of": as_of_date.isoformat() if as_of_date else None,
                    "note": "no positioning data",
                })
            return _text(payload)
        except Exception as exc:
            logger.warning(
                "get_taifex_positioning tool failed: %s %s", contract, exc,
            )
            return _text({"error": str(exc)})

    return [
        get_quote, compare_quotes, run_dcf, run_var, run_backtest,
        get_options_chain, get_symbol_news, get_symbol_sentiment,
        get_peers, get_financials,
        get_institutional_history, get_margin_history,
        get_top_brokers, get_taifex_positioning,
    ]
