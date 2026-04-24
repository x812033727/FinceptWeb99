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

from cache.redis_cache import cache_get, key_quote
from db.session import AsyncSessionLocal
from models.portfolio import Portfolio, PortfolioSnapshot

log = logging.getLogger(__name__)


async def take_all_snapshots() -> None:
    """Called by APScheduler once daily after all markets close (~23:00 UTC)."""
    from services.portfolio_service import _get_twd_usd_rate

    async with AsyncSessionLocal() as db:
        portfolios = list(
            (
                await db.scalars(
                    select(Portfolio).options(selectinload(Portfolio.holdings))
                )
            ).all()
        )

        today = date.today()
        twd_usd = await _get_twd_usd_rate()

        for portfolio in portfolios:
            total_usd = 0.0
            for holding in portfolio.holdings:
                market_key = holding.market.value.lower()
                cached = await cache_get(key_quote(market_key, holding.symbol))
                if not cached:
                    continue
                try:
                    price = json.loads(cached).get("price", 0) or 0
                except Exception:
                    continue

                value = float(holding.quantity) * price
                if holding.cost_currency == "TWD":
                    total_usd += value / twd_usd if twd_usd > 0 else 0
                else:
                    total_usd += value

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
            else:
                db.add(PortfolioSnapshot(
                    portfolio_id=portfolio.id,
                    snapshot_date=today,
                    total_value_usd=total_usd,
                ))

        try:
            await db.commit()
            log.info("portfolio_snapshot.taken", extra={"portfolios": len(portfolios), "date": today.isoformat()})
        except Exception as exc:
            log.error("portfolio_snapshot.failed", extra={"error": str(exc)})
            await db.rollback()
