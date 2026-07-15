"""
Daily EOD portfolio snapshot task.
Computes each portfolio's total market value in USD using cached quotes,
then upserts a PortfolioSnapshot row for today.
"""
import json
import logging
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from cache.redis_cache import acquire_lock, cache_get, key_quote, release_lock
from db.session import AsyncSessionLocal
from models.portfolio import Portfolio, PortfolioSnapshot

log = logging.getLogger(__name__)

# 23:00 UTC + a 30-minute window covers any clock skew between pods. The
# lock self-expires so a crashed holder never wedges tomorrow's run.
_SNAPSHOT_LOCK_KEY = "lock:portfolio_snapshot"
_SNAPSHOT_LOCK_TTL = 1800  # 30 minutes


async def take_all_snapshots() -> None:
    """Called by APScheduler once daily after all markets close (~23:00 UTC).

    In multi-pod deployments every pod's APScheduler will fire at the same
    time. The Redis lock ensures only ONE pod actually writes snapshots —
    the rest no-op.
    """
    if not await acquire_lock(_SNAPSHOT_LOCK_KEY, _SNAPSHOT_LOCK_TTL):
        log.info("portfolio_snapshot.skipped_lock_held")
        return

    try:
        await _do_snapshots()
    finally:
        await release_lock(_SNAPSHOT_LOCK_KEY)


async def _do_snapshots() -> None:
    from services.portfolio_cash_service import (
        cash_value_in_currency,
        get_cash_balances,
    )
    from services.portfolio_service import _to_portfolio_currency

    async with AsyncSessionLocal() as db:
        portfolios = list(
            (
                await db.scalars(
                    select(Portfolio).options(selectinload(Portfolio.holdings))
                )
            ).all()
        )

        today = date.today()
        for portfolio in portfolios:
            holdings_value_base = 0.0
            total_usd = 0.0
            positions: list[dict] = []
            missing_quotes: list[str] = []
            for holding in portfolio.holdings:
                market_key = holding.market.value.lower()
                cached = await cache_get(key_quote(market_key, holding.symbol))
                if not cached:
                    missing_quotes.append(holding.symbol)
                    continue
                try:
                    price = json.loads(cached).get("price", 0) or 0
                except Exception:
                    missing_quotes.append(holding.symbol)
                    continue
                if price <= 0:
                    missing_quotes.append(holding.symbol)
                    continue

                value = float(holding.quantity) * price
                value_base = await _to_portfolio_currency(
                    value, holding.cost_currency, portfolio.currency,
                )
                value_usd = await _to_portfolio_currency(
                    value, holding.cost_currency, "USD",
                )
                holdings_value_base += value_base
                total_usd += value_usd
                positions.append({
                    "symbol": holding.symbol, "market": holding.market.value,
                    "quantity": float(holding.quantity), "price": float(price),
                    "price_currency": holding.cost_currency,
                    "market_value_base": round(value_base, 2),
                })

            cash_balances = await get_cash_balances(
                portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id), db=db,
            )
            cash_value_base = await cash_value_in_currency(
                balances=cash_balances, target_currency=portfolio.currency,
            )
            cash_value_usd = await cash_value_in_currency(
                balances=cash_balances, target_currency="USD",
            )
            total_usd += cash_value_usd
            total_value_base = holdings_value_base + cash_value_base

            existing = await db.scalar(
                select(PortfolioSnapshot).where(
                    and_(
                        PortfolioSnapshot.portfolio_id == portfolio.id,
                        PortfolioSnapshot.snapshot_date == today,
                    )
                )
            )
            if existing:
                existing.total_value_usd = total_usd
                existing.base_currency = portfolio.currency
                existing.holdings_value_base = holdings_value_base
                existing.cash_value_base = cash_value_base
                existing.total_value_base = total_value_base
                existing.positions = positions
                existing.cash_balances = cash_balances
                existing.valuation_quality = {
                    "status": "complete" if not missing_quotes else "degraded",
                    "missing_quote_symbols": missing_quotes,
                }
            else:
                db.add(PortfolioSnapshot(
                    portfolio_id=portfolio.id,
                    snapshot_date=today,
                    total_value_usd=total_usd,
                    base_currency=portfolio.currency,
                    holdings_value_base=holdings_value_base,
                    cash_value_base=cash_value_base,
                    total_value_base=total_value_base,
                    positions=positions,
                    cash_balances=cash_balances,
                    valuation_quality={
                        "status": "complete" if not missing_quotes else "degraded",
                        "missing_quote_symbols": missing_quotes,
                    },
                ))

        try:
            await db.commit()
            log.info("portfolio_snapshot.taken", extra={"portfolios": len(portfolios), "date": today.isoformat()})
        except Exception as exc:
            log.error("portfolio_snapshot.failed", extra={"error": str(exc)})
            await db.rollback()
