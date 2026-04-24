"""
Portfolio service — CRUD, P&L calculation, multi-currency, optimisation.

Multi-currency rule:
  avg_cost and price are stored in cost_currency (TWD for TW stocks, USD for US).
  P&L is computed in cost_currency, then converted to portfolio.currency
  using the TWD/USD FX rate from FRED (series DEXTW).
  FX rate is cached 4h in Redis.
"""
import asyncio
import json
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from analytics.portfolio_optimizer import optimize, efficient_frontier
from cache.redis_cache import cache_get, cache_set
from data.us.fred_connector import get_latest
from models.portfolio import Holding, Portfolio, Transaction, TransactionType
from models.user import User
from services.us_market_service import get_quote as us_quote, get_history as us_history
from services.tw_market_service import get_quote as tw_quote, get_history as tw_history

TTL_FX = 4 * 3600
TTL_HISTORY = 4 * 3600


# ── FX helpers ────────────────────────────────────────────────────

async def _get_twd_usd_rate() -> float:
    """TWD per 1 USD. Cached 4h from FRED DEXTW series."""
    key = "fx:twd_usd"
    cached = await cache_get(key)
    if cached:
        return float(cached)
    rate = await get_latest("DEXTW") or 32.0   # fallback
    await cache_set(key, str(rate), TTL_FX)
    return rate


async def _to_portfolio_currency(amount: float, cost_currency: str, portfolio_currency: str) -> float:
    """Convert amount from cost_currency to portfolio_currency."""
    if cost_currency == portfolio_currency:
        return amount
    twd_usd = await _get_twd_usd_rate()
    if cost_currency == "TWD" and portfolio_currency == "USD":
        return amount / twd_usd
    if cost_currency == "USD" and portfolio_currency == "TWD":
        return amount * twd_usd
    return amount   # unsupported pair — return as-is


# ── CRUD ──────────────────────────────────────────────────────────

async def list_portfolios(user_id: str, db: AsyncSession) -> list[Portfolio]:
    rows = await db.scalars(
        select(Portfolio).where(Portfolio.user_id == UUID(user_id)).order_by(Portfolio.created_at)
    )
    return list(rows)


async def create_portfolio(user_id: str, name: str, currency: str, db: AsyncSession) -> Portfolio:
    p = Portfolio(user_id=UUID(user_id), name=name, currency=currency.upper())
    db.add(p)
    await db.flush()
    return p


async def get_portfolio(portfolio_id: str, user_id: str, db: AsyncSession) -> Portfolio | None:
    p = await db.get(Portfolio, UUID(portfolio_id))
    if not p or str(p.user_id) != user_id:
        return None
    return p


async def delete_portfolio(portfolio_id: str, user_id: str, db: AsyncSession) -> bool:
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        return False
    await db.delete(p)
    return True


# ── Transactions ──────────────────────────────────────────────────

async def add_transaction(
    portfolio_id: str,
    user_id: str,
    symbol: str,
    market: str,
    tx_type: str,
    quantity: float,
    price: float,
    fx_rate: float,
    tx_date: date,
    notes: str | None,
    db: AsyncSession,
) -> Transaction:
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        raise ValueError("Portfolio not found")

    from models.portfolio import Market as MarketEnum
    tx = Transaction(
        portfolio_id=UUID(portfolio_id),
        symbol=symbol.upper(),
        market=MarketEnum[market.upper()],
        tx_type=TransactionType[tx_type.lower()],
        quantity=quantity,
        price=price,
        fx_rate=fx_rate,
        tx_date=tx_date,
        notes=notes,
    )
    db.add(tx)
    await db.flush()

    # Rebuild holding from all transactions
    await _rebuild_holding(portfolio_id, symbol.upper(), market.upper(), db)
    return tx


async def _rebuild_holding(portfolio_id: str, symbol: str, market: str, db: AsyncSession) -> None:
    """Recalculate average cost and quantity from transaction history."""
    from models.portfolio import Market as MarketEnum
    txs = await db.scalars(
        select(Transaction)
        .where(
            Transaction.portfolio_id == UUID(portfolio_id),
            Transaction.symbol == symbol,
            Transaction.market == MarketEnum[market],
        )
        .order_by(Transaction.tx_date)
    )
    txs = list(txs)

    qty = 0.0
    cost_basis = 0.0
    for tx in txs:
        if tx.tx_type == TransactionType.buy:
            cost_basis = (cost_basis * qty + float(tx.price) * float(tx.quantity)) / (qty + float(tx.quantity))
            qty += float(tx.quantity)
        elif tx.tx_type == TransactionType.sell:
            qty = max(0.0, qty - float(tx.quantity))

    # Delete existing holding and recreate
    from models.portfolio import Market as MarketEnum
    existing = await db.scalar(
        select(Holding).where(
            Holding.portfolio_id == UUID(portfolio_id),
            Holding.symbol == symbol,
            Holding.market == MarketEnum[market],
        )
    )
    if existing:
        await db.delete(existing)

    if qty > 1e-9:
        cost_currency = "TWD" if market == "TW" else "USD"
        h = Holding(
            portfolio_id=UUID(portfolio_id),
            symbol=symbol,
            market=MarketEnum[market],
            quantity=qty,
            avg_cost=cost_basis,
            cost_currency=cost_currency,
        )
        db.add(h)


# ── P&L calculation ───────────────────────────────────────────────

async def get_portfolio_detail(portfolio_id: str, user_id: str, db: AsyncSession) -> dict[str, Any]:
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        raise ValueError("Portfolio not found")

    holdings = await db.scalars(select(Holding).where(Holding.portfolio_id == UUID(portfolio_id)))
    holdings = list(holdings)

    # Fetch current prices concurrently
    async def _enrich(h: Holding) -> dict:
        try:
            if str(h.market.value) == "US":
                q = await us_quote(h.symbol)
            else:
                q = await tw_quote(h.symbol)
            current_price = q.get("price", 0)
        except Exception:
            current_price = float(h.avg_cost)

        qty = float(h.quantity)
        avg = float(h.avg_cost)
        cost_val = qty * avg
        curr_val = qty * current_price

        # Convert to portfolio currency
        curr_val_pc = await _to_portfolio_currency(curr_val, h.cost_currency, p.currency)
        cost_val_pc = await _to_portfolio_currency(cost_val, h.cost_currency, p.currency)

        unrealized_pnl = curr_val_pc - cost_val_pc
        unrealized_pnl_pct = unrealized_pnl / cost_val_pc * 100 if cost_val_pc else 0.0

        return {
            "id": str(h.id),
            "symbol": h.symbol,
            "market": str(h.market.value),
            "quantity": qty,
            "avg_cost": avg,
            "cost_currency": h.cost_currency,
            "current_price": current_price,
            "current_value": round(curr_val_pc, 2),
            "cost_value": round(cost_val_pc, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 4),
        }

    enriched = await asyncio.gather(*[_enrich(h) for h in holdings])

    total_value = sum(h["current_value"] for h in enriched)
    total_cost  = sum(h["cost_value"]    for h in enriched)
    total_pnl   = total_value - total_cost
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0.0

    # Compute weight per holding
    for h in enriched:
        h["weight_pct"] = round(h["current_value"] / total_value * 100, 2) if total_value else 0.0

    return {
        "id": portfolio_id,
        "name": p.name,
        "currency": p.currency,
        "total_value": round(total_value, 2),
        "total_cost":  round(total_cost, 2),
        "total_pnl":   round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "holdings": sorted(enriched, key=lambda x: x["current_value"], reverse=True),
    }


# ── Optimisation ──────────────────────────────────────────────────

async def optimise_portfolio(
    portfolio_id: str,
    user_id: str,
    target_risk: str,
    max_weight: float,
    db: AsyncSession,
) -> dict[str, Any]:
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        raise ValueError("Portfolio not found")

    holdings = list(await db.scalars(select(Holding).where(Holding.portfolio_id == UUID(portfolio_id))))
    if not holdings:
        raise ValueError("Portfolio has no holdings")

    # Fetch 252 days of daily returns for each holding
    async def _get_returns(h: Holding) -> tuple[str, list[float]]:
        try:
            if str(h.market.value) == "US":
                bars = await us_history(h.symbol, period="1y", interval="1d")
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
    from datetime import date, timedelta
    from sqlalchemy import and_, select
    from models.portfolio import PortfolioSnapshot

    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        return []

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


async def get_transactions(
    portfolio_id: str, user_id: str, db: AsyncSession, limit: int = 200
) -> list[Transaction]:
    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        return []
    rows = await db.scalars(
        select(Transaction)
        .where(Transaction.portfolio_id == portfolio.id)
        .order_by(Transaction.tx_date.desc(), Transaction.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())
