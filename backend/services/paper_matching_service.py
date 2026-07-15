"""Quote-driven matching rules for paper orders."""

from __future__ import annotations

import math
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperFill, PaperOrder
from services import paper_trading_service


class MarketClosedError(paper_trading_service.PaperTradingConflict):
    """The order cannot match outside its market session."""


def is_market_open(market: str, at: datetime) -> bool:
    if at.tzinfo is None:
        raise ValueError("at must be timezone-aware")
    if market == "CRYPTO":
        return True
    timezone, opens, closes = (
        (ZoneInfo("Asia/Taipei"), time(9), time(13, 30))
        if market == "TW"
        else (ZoneInfo("America/New_York"), time(9, 30), time(16))
    )
    local = at.astimezone(timezone)
    return local.weekday() < 5 and opens <= local.time() < closes


def executable_price(order: PaperOrder, quote: dict) -> float | None:
    field = "ask" if order.side == "buy" else "bid"
    raw = quote.get(field) or quote.get("price") or quote.get("close")
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    if order.order_type == "market":
        return price
    limit = float(order.limit_price)
    if order.side == "buy" and price <= limit:
        return price
    if order.side == "sell" and price >= limit:
        return price
    return None


async def _get_quote(order: PaperOrder) -> dict:
    if order.market == "TW":
        from services.tw_market_service import get_quote
    elif order.market == "US":
        from services.us_market_service import get_quote
    else:
        from services.crypto_market_service import get_quote
    return await get_quote(order.symbol)


async def match_order(
    *,
    portfolio_id: str,
    order_id: str,
    user_id: str,
    db: AsyncSession,
    now: datetime | None = None,
    quote: dict | None = None,
) -> PaperFill | None:
    execution_time = now or datetime.now(UTC)
    if execution_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    order = await paper_trading_service.get_order(
        portfolio_id=portfolio_id,
        order_id=order_id,
        user_id=user_id,
        db=db,
        lock=True,
    )
    if order.status not in paper_trading_service.OPEN_STATUSES:
        raise paper_trading_service.PaperTradingConflict(f"order is already {order.status}")
    if order.expires_at and execution_time >= order.expires_at:
        order.status = "expired"
        order.expired_at = execution_time
        order.updated_at = execution_time
        await db.flush()
        return None
    if not is_market_open(order.market, execution_time):
        raise MarketClosedError(f"{order.market} market is closed")
    market_quote = quote if quote is not None else await _get_quote(order)
    price = executable_price(order, market_quote)
    if price is None:
        return None
    remaining = float(order.quantity) - float(order.filled_quantity)
    stamp = execution_time.astimezone(UTC).isoformat(timespec="microseconds")
    return await paper_trading_service.fill_order(
        portfolio_id=portfolio_id,
        order_id=order_id,
        user_id=user_id,
        quantity=remaining,
        price=price,
        idempotency_key=f"quote-match:{stamp}",
        filled_at=execution_time,
        db=db,
    )
