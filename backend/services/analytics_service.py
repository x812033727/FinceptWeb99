"""
Analytics service — fetches market data then delegates to pure analytics modules.
Long-running tasks (Monte Carlo, backtest) run in ProcessPoolExecutor.
"""
import asyncio
import functools
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np
import pandas as pd

from analytics.dcf import run_dcf
from analytics.risk import var_historical, var_parametric, var_monte_carlo, portfolio_metrics
from analytics.backtest import run_backtest
from services.us_market_service import get_history as us_history, get_fundamentals as us_fundamentals
from services.tw_market_service import get_history as tw_history

_executor: ProcessPoolExecutor | None = None
_TIMEOUT = 30.0   # seconds max for heavy computations


def _get_executor() -> ProcessPoolExecutor:
    """Create the process pool lazily so importing this module stays lightweight."""
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(max_workers=2)
    return _executor


# ── DCF ───────────────────────────────────────────────────────────

async def run_dcf_analysis(
    symbol: str,
    market: str,
    overrides: dict | None = None,
) -> dict[str, Any]:
    overrides = overrides or {}

    # Fetch fundamentals for auto-fill of FCF, shares, net_debt
    try:
        if market.upper() == "US":
            info = await us_fundamentals(symbol)
            shares = info.get("share_class_shares_outstanding") or 1e9
        else:
            info = {}
            shares = float(overrides.get("shares", 1e9))
    except Exception:
        info = {}
        shares = float(overrides.get("shares", 1e9))

    # Caller must provide at minimum: fcf_history or base_fcf
    fcf_history = overrides.get("fcf_history") or [float(overrides.get("base_fcf", 1e9))]
    current_price = overrides.get("current_price")

    result = run_dcf(
        fcf_history=fcf_history,
        growth_rate_1=float(overrides.get("growth_rate_1", 0.10)),
        growth_rate_2=float(overrides.get("growth_rate_2", 0.05)),
        terminal_growth=float(overrides.get("terminal_growth", 0.03)),
        wacc=float(overrides.get("wacc", 0.09)),
        shares=float(overrides.get("shares", shares)),
        net_debt=float(overrides.get("net_debt", 0.0)),
        current_price=float(current_price) if current_price else None,
    )
    result["symbol"] = symbol
    result["market"] = market
    return result


# ── VaR ───────────────────────────────────────────────────────────

async def _fetch_returns(symbols: list[str], markets: list[str]) -> dict[str, np.ndarray]:
    """Fetch 1y daily history and compute returns for each symbol."""
    out: dict[str, np.ndarray] = {}
    for sym, mkt in zip(symbols, markets):
        try:
            if mkt.upper() == "US":
                bars = await us_history(sym, period="1y", interval="1d")
            else:
                bars = await tw_history(sym, months=12)
            closes = np.array([b["close"] for b in bars if b.get("close")], dtype=float)
            if len(closes) < 20:
                continue
            out[sym] = np.diff(closes) / closes[:-1]
        except Exception:
            continue
    return out


async def run_var_analysis(
    symbols: list[str],
    markets: list[str],
    weights: list[float],
    portfolio_value: float,
    method: str = "historical",
    confidence: float = 0.95,
    horizon_days: int = 1,
) -> dict[str, Any]:
    returns_map = await _fetch_returns(symbols, markets)
    if not returns_map:
        return {"error": "No price data available"}

    # Align lengths
    valid_symbols = [s for s in symbols if s in returns_map]
    if not valid_symbols:
        return {"error": "No valid symbols"}

    min_len = min(len(returns_map[s]) for s in valid_symbols)
    aligned = np.column_stack([returns_map[s][-min_len:] for s in valid_symbols])

    # Normalise weights
    w = np.array([weights[symbols.index(s)] for s in valid_symbols], dtype=float)
    w /= w.sum()

    port_returns = aligned @ w

    results: dict[str, Any] = {}

    if method in ("historical", "all"):
        results["historical"] = var_historical(port_returns, portfolio_value, confidence, horizon_days)
    if method in ("parametric", "all"):
        results["parametric"] = var_parametric(port_returns, portfolio_value, confidence, horizon_days)
    if method in ("monte_carlo", "all"):
        # Run in executor to avoid blocking the event loop. Pass positional
        # args (not a lambda) so the callable is picklable for the worker
        # process — lambdas aren't picklable under any mp start method.
        loop = asyncio.get_running_loop()
        results["monte_carlo"] = await asyncio.wait_for(
            loop.run_in_executor(
                _get_executor(),
                var_monte_carlo, aligned, w, portfolio_value, confidence, horizon_days,
            ),
            timeout=_TIMEOUT,
        )

    results["portfolio_metrics"] = portfolio_metrics(port_returns)
    results["symbols"] = valid_symbols
    results["weights"] = w.tolist()
    return results


# ── Backtest ──────────────────────────────────────────────────────

async def run_backtest_analysis(
    symbols: list[str],
    markets: list[str],
    strategy: str,
    params: dict,
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
    *,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    position_size_pct: float | None = None,
    slippage_bps: float = 0.0,
    commission_bps: float = 0.0,
    allow_short: bool = False,
) -> dict[str, Any]:
    # Fetch price history for all symbols
    prices: dict[str, list] = {}
    for sym, mkt in zip(symbols, markets):
        try:
            if mkt.upper() == "US":
                bars = await us_history(sym, period="5y", interval="1d")
            else:
                bars = await tw_history(sym, months=60)
            prices[sym] = bars
        except Exception:
            pass

    if not prices:
        return {"status": "failed", "error": "No price data"}

    # Build aligned DataFrame filtered to [start_date, end_date]
    series_map: dict[str, dict] = {}
    for sym, bars in prices.items():
        for b in bars:
            d = str(b.get("time", ""))[:10]
            if start_date <= d <= end_date and b.get("close"):
                series_map.setdefault(sym, {})[d] = float(b["close"])

    if not series_map:
        return {"status": "failed", "error": "No data in date range"}

    # Align to common dates
    all_dates = sorted(set().union(*[s.keys() for s in series_map.values()]))
    df = pd.DataFrame(
        {sym: [series_map[sym].get(d) for d in all_dates] for sym in series_map},
        index=all_dates,
    ).ffill().dropna()

    # Update strategy params with symbol list
    params = {**params, "symbols": list(df.columns)}

    # Run in executor (can be CPU-heavy for 5-year multi-asset). Use
    # functools.partial (picklable, unlike a lambda) so the C2 risk-
    # control keyword args reach the worker process.
    call = functools.partial(
        run_backtest, df, strategy, params, initial_capital,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
        position_size_pct=position_size_pct,
        slippage_bps=slippage_bps,
        commission_bps=commission_bps,
        allow_short=allow_short,
    )
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(
        loop.run_in_executor(_get_executor(), call),
        timeout=_TIMEOUT,
    )
    return result
