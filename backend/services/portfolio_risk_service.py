"""Portfolio risk dashboard — three-method VaR / correlation /
concentration on the user's ACTUAL holdings (feature C1).

The stateless ``POST /api/analytics/var`` endpoint already exposes the
three VaR engines in ``analytics/risk.py``, but the caller has to type
symbols + weights by hand. This service closes the loop: it loads the
portfolio (owner-scoped), derives the weights from live market values
(reusing ``get_portfolio_detail`` so FX conversion / degraded-quote
handling stay in ONE place), pulls 1y of daily history per holding
(same connectors ``optimise_portfolio`` uses), and delegates every
number to the existing pure functions in ``analytics/risk.py`` —
NOTHING is re-implemented here.

Design notes:
  - No new tables. Everything is computed on demand; the frontend
    caches with a 5-minute staleTime.
  - Monte Carlo runs in the shared ``ProcessPoolExecutor`` owned by
    ``services.analytics_service`` (same pattern as ``/analytics/var``)
    so 10k simulations never block the event loop.
  - Degrades gracefully: holdings with < MIN_OBSERVATIONS days of
    usable history land in ``excluded`` (with a reason) instead of
    failing the response; an empty portfolio returns an explicit
    empty-state payload with HTTP 200.
  - Concentration warnings: single position > 25 % and market bucket
    (US / TW / CRYPTO) > 50 %. ``broker_concentration_service`` was
    evaluated for reuse but it models TW branch-broker (主力分點)
    accumulation — a per-symbol trading signal, not portfolio-weight
    concentration — so it does not fit here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import numpy as np

from analytics.risk import (
    portfolio_metrics,
    var_historical,
    var_monte_carlo,
    var_parametric,
)
from services import analytics_service
from services.crypto_market_service import get_history as crypto_history
from services.tw_market_service import get_history as tw_history
from services.us_market_service import get_history as us_history

log = logging.getLogger(__name__)

# risk.py needs >=20 obs (historical/parametric) and >=30 (Monte Carlo).
# Require 30 so every included holding feeds all three methods.
MIN_OBSERVATIONS = 30
CONFIDENCE_LEVELS = (0.95, 0.99)
SINGLE_POSITION_THRESHOLD_PCT = 25.0
MARKET_BUCKET_THRESHOLD_PCT = 50.0


async def _fetch_holding_returns(symbol: str, market: str) -> np.ndarray:
    """1y of daily simple returns for one holding. Empty array on any
    connector failure or insufficient bars — caller decides exclusion."""
    try:
        mkt = market.upper()
        if mkt == "US":
            bars = await us_history(symbol, period="1y", interval="1d")
        elif mkt == "CRYPTO":
            bars = await crypto_history(symbol, interval="1d", limit=365)
        else:
            bars = await tw_history(symbol, months=12)
        closes = np.array(
            [b["close"] for b in bars if b.get("close")], dtype=float,
        )
        if len(closes) < 2:
            return np.array([])
        return np.diff(closes) / closes[:-1]
    except Exception:
        log.warning(
            "portfolio_risk.history_fetch_failed",
            extra={"symbol": symbol, "market": market},
            exc_info=True,
        )
        return np.array([])


async def _fetch_benchmark_returns(currency: str) -> tuple[str | None, np.ndarray]:
    """Benchmark daily returns matching the portfolio currency.

    Mirrors the PerformanceChart convention (PR #191): TWD portfolios
    compare against the TAIEX total-return index (``_TAIEX_TR`` rows in
    ohlcv_daily), everything else against SPY.
    """
    if (currency or "").upper() == "TWD":
        symbol, market = "_TAIEX_TR", "TW"
    else:
        symbol, market = "SPY", "US"
    returns = await _fetch_holding_returns(symbol, market)
    if len(returns) < MIN_OBSERVATIONS:
        return None, np.array([])
    return symbol, returns


def _concentration_warnings(weights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Single-position > 25 % and market-bucket > 50 % checks."""
    warnings: list[dict[str, Any]] = []
    for w in weights:
        if w["weight_pct"] > SINGLE_POSITION_THRESHOLD_PCT:
            warnings.append({
                "kind": "single_position",
                "key": w["symbol"],
                "weight_pct": w["weight_pct"],
                "threshold_pct": SINGLE_POSITION_THRESHOLD_PCT,
            })
    buckets: dict[str, float] = {}
    for w in weights:
        buckets[w["market"]] = buckets.get(w["market"], 0.0) + w["weight_pct"]
    for market, pct in sorted(buckets.items()):
        if pct > MARKET_BUCKET_THRESHOLD_PCT:
            warnings.append({
                "kind": "market_bucket",
                "key": market,
                "weight_pct": round(pct, 2),
                "threshold_pct": MARKET_BUCKET_THRESHOLD_PCT,
            })
    return warnings


def _empty_payload(portfolio_id: str, p_currency: str, portfolio_value: float = 0.0) -> dict[str, Any]:
    return {
        "portfolio_id": portfolio_id,
        "currency": p_currency,
        "as_of": date.today().isoformat(),
        "portfolio_value": round(portfolio_value, 2),
        "observations": 0,
        "empty": True,
        "benchmark": None,
        "metrics": None,
        "var": [],
        "weights": [],
        "correlation": None,
        "warnings": [],
        "excluded": [],
    }


async def get_portfolio_risk(
    portfolio_id: str,
    user_id: str,
    db,
) -> dict[str, Any]:
    """Compute the full risk dashboard payload for one portfolio.

    Raises ``ValueError("Portfolio not found")`` when the portfolio does
    not exist or belongs to another user (router maps it to 404 — same
    contract as ``get_performance`` / ``get_portfolio_detail``).
    """
    from services.portfolio_service import get_portfolio_detail

    # Ownership check + live market values / weights in ONE call —
    # raises ValueError for missing/foreign portfolios.
    detail = await get_portfolio_detail(portfolio_id, user_id, db)

    holdings = detail["holdings"]
    total_value = float(detail["total_value"])
    if not holdings or total_value <= 0:
        return _empty_payload(portfolio_id, detail["currency"], total_value)

    # ── history fan-out ───────────────────────────────────────────
    returns_list = await asyncio.gather(
        *[_fetch_holding_returns(h["symbol"], h["market"]) for h in holdings]
    )

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for h, rets in zip(holdings, returns_list):
        if len(rets) >= MIN_OBSERVATIONS:
            included.append({"holding": h, "returns": rets})
        else:
            excluded.append({
                "symbol": h["symbol"],
                "market": h["market"],
                "reason": "insufficient_history",
                "observations": int(len(rets)),
            })

    # Weight metadata covers ALL holdings (excluded ones still count
    # toward concentration — a 40 % position is risky even without a
    # return series). Risk math below uses included holdings only.
    weight_rows: list[dict[str, Any]] = [
        {
            "symbol": h["symbol"],
            "market": h["market"],
            "weight_pct": float(h["weight_pct"]),
            "risk_contribution_pct": None,
        }
        for h in holdings
    ]
    warnings = _concentration_warnings(weight_rows)

    if not included:
        payload = _empty_payload(portfolio_id, detail["currency"], total_value)
        payload["empty"] = False
        payload["weights"] = weight_rows
        payload["warnings"] = warnings
        payload["excluded"] = excluded
        return payload

    # ── align + portfolio return series ───────────────────────────
    min_len = min(len(item["returns"]) for item in included)
    aligned = np.column_stack([item["returns"][-min_len:] for item in included])
    raw_weights = np.array(
        [item["holding"]["weight_pct"] for item in included], dtype=float,
    )
    w = raw_weights / raw_weights.sum()
    port_returns = aligned @ w

    # ── risk contribution (marginal contribution to variance) ─────
    cov = np.cov(aligned.T)
    if cov.ndim == 0:
        cov = np.array([[float(cov)]])
    port_var = float(w @ cov @ w)
    contrib = (w * (cov @ w)) / port_var if port_var > 1e-18 else w
    contrib_by_symbol = {
        item["holding"]["symbol"]: round(float(c) * 100, 2)
        for item, c in zip(included, contrib)
    }
    for row in weight_rows:
        row["risk_contribution_pct"] = contrib_by_symbol.get(row["symbol"])

    # ── VaR: three existing methods × two confidence levels ───────
    var_results: list[dict[str, Any]] = []
    for conf in CONFIDENCE_LEVELS:
        var_results.append(var_historical(port_returns, total_value, conf))
        var_results.append(var_parametric(port_returns, total_value, conf))

    # Monte Carlo in the shared process pool (same executor + timeout
    # /analytics/var uses) so 10k sims don't block the event loop.
    loop = asyncio.get_running_loop()
    for conf in CONFIDENCE_LEVELS:
        try:
            mc = await asyncio.wait_for(
                loop.run_in_executor(
                    analytics_service._get_executor(),
                    var_monte_carlo, aligned, w, total_value, conf,
                ),
                timeout=analytics_service._TIMEOUT,
            )
            var_results.append(mc)
        except Exception:
            log.warning(
                "portfolio_risk.monte_carlo_failed",
                extra={"portfolio_id": portfolio_id, "confidence": conf},
                exc_info=True,
            )
    var_results = [v for v in var_results if v]   # risk.py returns {} on thin data

    # ── portfolio metrics + beta vs benchmark ─────────────────────
    benchmark_symbol, bench_returns = await _fetch_benchmark_returns(detail["currency"])
    bench_aligned = None
    if benchmark_symbol and len(bench_returns) > 0:
        common = min(len(port_returns), len(bench_returns))
        metrics = portfolio_metrics(
            port_returns[-common:], bench_returns[-common:],
        )
        bench_aligned = benchmark_symbol
    else:
        metrics = portfolio_metrics(port_returns)

    # ── correlation matrix ────────────────────────────────────────
    corr_symbols = [item["holding"]["symbol"] for item in included]
    if len(included) > 1:
        corr = np.corrcoef(aligned.T)
    else:
        corr = np.array([[1.0]])
    # NaN guard: a zero-variance series (e.g. stale close repeated)
    # yields NaN correlations — clamp to 0 so JSON stays valid.
    corr = np.nan_to_num(corr, nan=0.0)
    correlation = {
        "symbols": corr_symbols,
        "matrix": [[round(float(c), 4) for c in row] for row in corr],
    }

    return {
        "portfolio_id": portfolio_id,
        "currency": detail["currency"],
        "as_of": date.today().isoformat(),
        "portfolio_value": round(total_value, 2),
        "observations": int(min_len),
        "empty": False,
        "benchmark": bench_aligned,
        "metrics": metrics or None,
        "var": var_results,
        "weights": weight_rows,
        "correlation": correlation,
        "warnings": warnings,
        "excluded": excluded,
    }
