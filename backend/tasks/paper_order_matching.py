"""Automatic paper-order matcher with per-order transaction isolation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from middleware.metrics import PAPER_ORDER_MATCH_OUTCOMES_TOTAL
from models.paper_trading import PaperOrder
from models.portfolio import Portfolio
from services import paper_matching_service, paper_trading_service

log = logging.getLogger(__name__)

LOCK_KEY = "lock:paper_order_matching"
LOCK_TTL_SECONDS = 120
BATCH_SIZE = 500


@dataclass
class MatchRunStats:
    scanned: int = 0
    matched: int = 0
    expired: int = 0
    untriggered: int = 0
    closed: int = 0
    stale: int = 0
    conflicted: int = 0
    failed: int = 0
    lock_held: bool = False


def _observe(stats: MatchRunStats) -> None:
    for outcome in (
        "matched",
        "expired",
        "untriggered",
        "closed",
        "stale",
        "conflicted",
        "failed",
    ):
        count = getattr(stats, outcome)
        if count:
            PAPER_ORDER_MATCH_OUTCOMES_TOTAL.labels(outcome).inc(count)


async def _candidate_rows() -> list[tuple]:
    async with AsyncSessionLocal() as db:
        return list(
            (
                await db.execute(
                    select(
                        PaperOrder.id,
                        PaperOrder.portfolio_id,
                        Portfolio.user_id,
                        PaperOrder.market,
                        PaperOrder.symbol,
                        PaperOrder.expires_at,
                    )
                    .join(Portfolio, Portfolio.id == PaperOrder.portfolio_id)
                    .where(PaperOrder.status.in_(paper_trading_service.OPEN_STATUSES))
                    .order_by(PaperOrder.created_at, PaperOrder.id)
                    .limit(BATCH_SIZE)
                )
            ).all()
        )


async def match_open_paper_orders(*, now: datetime | None = None) -> MatchRunStats:
    """Evaluate one bounded batch; failures are isolated to a single order."""
    stats = MatchRunStats()
    execution_time = now or datetime.now(UTC)
    if execution_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not await acquire_lock(LOCK_KEY, LOCK_TTL_SECONDS):
        stats.lock_held = True
        log.info("paper_order_matching.skipped_lock_held")
        return stats

    quote_cache: dict[tuple[str, str], dict] = {}
    try:
        candidates = await _candidate_rows()
        stats.scanned = len(candidates)
        for order_id, portfolio_id, user_id, market, symbol, expires_at in candidates:
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            expired = expires_at is not None and execution_time >= expires_at
            if not expired and not paper_matching_service.is_market_open(market, execution_time):
                stats.closed += 1
                continue

            try:
                quote: dict = {}
                if not expired:
                    key = (market, symbol)
                    if key not in quote_cache:
                        quote_cache[key] = await paper_matching_service.get_market_quote(
                            market, symbol
                        )
                    quote = quote_cache[key]

                async with AsyncSessionLocal() as db:
                    try:
                        fill = await paper_matching_service.match_order(
                            portfolio_id=str(portfolio_id),
                            order_id=str(order_id),
                            user_id=str(user_id),
                            db=db,
                            now=execution_time,
                            quote=quote,
                        )
                        order = await db.get(PaperOrder, order_id)
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise
                if fill is not None:
                    stats.matched += 1
                elif order is not None and order.status == "expired":
                    stats.expired += 1
                else:
                    stats.untriggered += 1
            except paper_matching_service.StaleQuoteError as exc:
                stats.stale += 1
                log.info(
                    "paper_order_matching.stale_quote",
                    extra={"order_id": str(order_id), "error": str(exc)},
                )
            except paper_trading_service.PaperTradingConflict as exc:
                stats.conflicted += 1
                log.info(
                    "paper_order_matching.conflicted",
                    extra={"order_id": str(order_id), "error": str(exc)},
                )
            except Exception as exc:
                stats.failed += 1
                log.warning(
                    "paper_order_matching.failed",
                    extra={"order_id": str(order_id), "error": str(exc)},
                )

        _observe(stats)
        log.info("paper_order_matching.done", extra=stats.__dict__)
        return stats
    finally:
        await release_lock(LOCK_KEY)
