"""Quote-driven matching rules for paper orders."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperFill, PaperOrder
from services import paper_trading_service


class MarketClosedError(paper_trading_service.PaperTradingConflict):
    """The order cannot match outside its market session."""


MAX_VOLUME_PARTICIPATION = 0.01
BASE_SLIPPAGE_BPS = 2.0
IMPACT_SLIPPAGE_BPS = 8.0


@dataclass(frozen=True)
class ExecutionPlan:
    quantity: float
    price: float
    quote_price: float
    slippage_bps: float
    liquidity_quantity: float
    quote_key: str


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


def _positive(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def quote_identity(market: str, symbol: str, quote: dict) -> str:
    payload = {
        key: quote.get(key)
        for key in (
            "ts",
            "timestamp",
            "as_of",
            "price",
            "close",
            "bid",
            "ask",
            "bid_size",
            "ask_size",
            "volume",
            "data_source",
        )
        if quote.get(key) is not None
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return f"{market}:{symbol}:{digest}"


def plan_execution(order: PaperOrder, quote: dict) -> ExecutionPlan | None:
    quote_price = executable_price(order, quote)
    if quote_price is None:
        return None
    remaining = max(0.0, float(order.quantity) - float(order.filled_quantity or 0))
    if remaining <= 1e-9:
        return None

    size_field = "ask_size" if order.side == "buy" else "bid_size"
    liquidity = _positive(quote.get(size_field)) or _positive(quote.get("size"))
    if liquidity is None:
        volume = _positive(quote.get("volume"))
        liquidity = volume * MAX_VOLUME_PARTICIPATION if volume else remaining
    quantity = min(remaining, liquidity)
    if quantity < 1e-6:
        return None
    utilization = min(1.0, quantity / liquidity)
    requested_slippage = BASE_SLIPPAGE_BPS + IMPACT_SLIPPAGE_BPS * utilization
    multiplier = 1 + requested_slippage / 10_000 * (1 if order.side == "buy" else -1)
    price = quote_price * multiplier
    if order.order_type == "limit":
        limit = float(order.limit_price)
        price = min(price, limit) if order.side == "buy" else max(price, limit)
    actual_slippage = abs(price - quote_price) / quote_price * 10_000
    return ExecutionPlan(
        quantity=quantity,
        price=price,
        quote_price=quote_price,
        slippage_bps=actual_slippage,
        liquidity_quantity=liquidity,
        quote_key=quote_identity(order.market, order.symbol, quote),
    )


async def get_market_quote(market: str, symbol: str) -> dict:
    """Resolve a normalized quote through the market's existing cache waterfall."""
    if market == "TW":
        from services.tw_market_service import get_quote
    elif market == "US":
        from services.us_market_service import get_quote
    elif market == "CRYPTO":
        from services.crypto_market_service import get_quote
    else:
        raise ValueError(f"unsupported market {market}")
    return await get_quote(symbol)


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
    expires_at = order.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at and execution_time >= expires_at:
        order.status = "expired"
        order.expired_at = execution_time
        order.updated_at = execution_time
        await db.flush()
        return None
    if not is_market_open(order.market, execution_time):
        raise MarketClosedError(f"{order.market} market is closed")
    market_quote = (
        quote if quote is not None else await get_market_quote(order.market, order.symbol)
    )
    plan = plan_execution(order, market_quote)
    if plan is None:
        return None
    idempotency_key = f"quote-match:{plan.quote_key}"
    existing = await db.scalar(
        select(PaperFill).where(
            PaperFill.order_id == order.id,
            PaperFill.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return None
    return await paper_trading_service.fill_order(
        portfolio_id=portfolio_id,
        order_id=order_id,
        user_id=user_id,
        quantity=plan.quantity,
        price=plan.price,
        idempotency_key=idempotency_key,
        filled_at=execution_time,
        db=db,
        quote_price=plan.quote_price,
        slippage_bps=plan.slippage_bps,
        liquidity_quantity=plan.liquidity_quantity,
        quote_key=plan.quote_key,
        execution_source="quote",
    )
