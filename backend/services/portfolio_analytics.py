"""Portfolio analytics — Markowitz optimiser + daily-snapshot history.

Two surface functions that were appended to ``portfolio_service`` over
time but live in their own concern: they don't touch the CRUD or
holdings-rebuild paths, they only read aggregated data and compute
or fetch summaries.

  - ``optimise_portfolio`` — fetch 252 days of returns per holding
    (US / TW / crypto), align by length, then run the Markowitz
    optimiser from ``analytics.portfolio_optimizer`` plus the
    efficient-frontier sweep. Pure read.
  - ``get_performance`` — pull the rolling-N-days
    ``PortfolioSnapshot`` rows (populated daily by the
    ``portfolio_snapshot`` cron) and return the date / total-value
    pairs the dashboard chart needs.

Both functions need ``get_portfolio`` from the still-monolithic
``portfolio_service`` (owner-scope check). Lazy-import to avoid the
circular: ``portfolio_service`` re-exports both names for back-compat
with ``api/portfolio/router.py``.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.portfolio_optimizer import efficient_frontier, optimize
from models.portfolio import Holding, PortfolioSnapshot
from services.crypto_market_service import get_history as crypto_history
from services.tw_market_service import get_history as tw_history
from services.us_market_service import get_history as us_history


async def optimise_portfolio(
    portfolio_id: str,
    user_id: str,
    target_risk: str,
    max_weight: float,
    db: AsyncSession,
) -> dict[str, Any]:
    from services.portfolio_service import get_portfolio
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        raise ValueError("Portfolio not found")

    holdings = list(await db.scalars(select(Holding).where(Holding.portfolio_id == UUID(portfolio_id))))
    if not holdings:
        raise ValueError("Portfolio has no holdings")

    # Fetch 252 days of daily returns for each holding
    async def _get_returns(h: Holding) -> tuple[str, list[float]]:
        try:
            mkt = str(h.market.value)
            if mkt == "US":
                bars = await us_history(h.symbol, period="1y", interval="1d")
            elif mkt == "CRYPTO":
                bars = await crypto_history(h.symbol, interval="1d", limit=365)
            else:
                bars = await tw_history(h.symbol, months=12)
            closes = [b["close"] for b in bars if b.get("close")]
            if len(closes) < 20:
                return h.symbol, []
            import numpy as np
            returns = list(np.diff(closes) / closes[:-1])
            return h.symbol, returns
        except Exception:
            return h.symbol, []

    results = await asyncio.gather(*[_get_returns(h) for h in holdings])

    # Build aligned returns DataFrame
    series = {sym: rets for sym, rets in results if rets}
    if not series:
        raise ValueError("Insufficient price history for optimisation")

    min_len = min(len(v) for v in series.values())
    aligned = {sym: rets[-min_len:] for sym, rets in series.items()}
    returns_df = pd.DataFrame(aligned)

    result = optimize(
        returns_df,
        constraints={"target_risk": target_risk, "max_weight": max_weight, "min_weight": 0.0},
    )
    result["frontier"] = efficient_frontier(returns_df, n_points=20)
    return result


async def get_performance(
    portfolio_id: str,
    user_id: str,
    db: AsyncSession,
    days: int = 90,
) -> list[dict]:
    """Return daily value snapshots for the portfolio over the last N days."""
    from services.portfolio_service import get_portfolio
    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")

    cutoff = date.today() - timedelta(days=days)
    rows = list(
        (
            await db.scalars(
                select(PortfolioSnapshot)
                .where(
                    and_(
                        PortfolioSnapshot.portfolio_id == portfolio.id,
                        PortfolioSnapshot.snapshot_date >= cutoff,
                    )
                )
                .order_by(PortfolioSnapshot.snapshot_date)
            )
        ).all()
    )
    return [{"date": r.snapshot_date.isoformat(), "value": r.total_value_usd} for r in rows]
