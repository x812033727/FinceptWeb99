"""Owner-scoped paper order lifecycle backed by real portfolio accounting."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperFill, PaperOrder
from models.portfolio import Holding, Market, Portfolio

OPEN_STATUSES = ("pending", "partially_filled")


class PaperTradingConflict(ValueError):
    """The request is valid but conflicts with funds, inventory, or order state."""


def _finite_positive(value: float, field: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return parsed


async def _owned_portfolio_locked(
    portfolio_id: str,
    user_id: str,
    db: AsyncSession,
) -> Portfolio:
    portfolio = await db.scalar(
        select(Portfolio)
        .where(
            Portfolio.id == UUID(portfolio_id),
            Portfolio.user_id == UUID(user_id),
        )
        .with_for_update()
    )
    if not portfolio:
        raise ValueError("Portfolio not found")
    return portfolio


def _currency(market: str) -> str:
    return "TWD" if market == "TW" else "USD"


async def _open_orders(portfolio_id: str, db: AsyncSession) -> list[PaperOrder]:
    return list(
        (
            await db.scalars(
                select(PaperOrder).where(
                    PaperOrder.portfolio_id == UUID(portfolio_id),
                    PaperOrder.status.in_(OPEN_STATUSES),
                )
            )
        ).all()
    )


async def _cash_balance(portfolio_id: str, currency: str, db: AsyncSession) -> float:
    from sqlalchemy import func

    from models.portfolio import PortfolioCashEntry

    amount = await db.scalar(
        select(func.sum(PortfolioCashEntry.amount)).where(
            PortfolioCashEntry.portfolio_id == UUID(portfolio_id),
            PortfolioCashEntry.currency == currency,
        )
    )
    return float(amount or 0)


def _remaining(order: PaperOrder) -> float:
    return max(0.0, float(order.quantity) - float(order.filled_quantity))


def _buy_reservation(order: PaperOrder, remaining: float | None = None) -> float:
    qty = _remaining(order) if remaining is None else remaining
    return qty * float(order.reservation_price) * (1 + float(order.fee_bps) / 10_000)


def _day_expiry(market: str, submitted_at: datetime) -> datetime:
    if market == "CRYPTO":
        return datetime.combine(
            submitted_at.astimezone(UTC).date() + timedelta(days=1), time(), UTC
        )
    timezone, close = (
        (ZoneInfo("Asia/Taipei"), time(13, 30))
        if market == "TW"
        else (ZoneInfo("America/New_York"), time(16))
    )
    local = submitted_at.astimezone(timezone)
    session_date = local.date()
    if local.weekday() >= 5 or local.time() >= close:
        session_date += timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date += timedelta(days=1)
    return datetime.combine(session_date, close, timezone).astimezone(UTC)


async def submit_order(
    *,
    portfolio_id: str,
    user_id: str,
    symbol: str,
    market: str,
    side: str,
    order_type: str,
    quantity: float,
    limit_price: float | None,
    reference_price: float | None,
    fee_bps: float,
    idempotency_key: str,
    notes: str | None,
    db: AsyncSession,
    time_in_force: str = "day",
) -> PaperOrder:
    await _owned_portfolio_locked(portfolio_id, user_id, db)
    normalized_symbol = symbol.strip().upper()
    normalized_market = market.upper()
    normalized_side = side.lower()
    normalized_type = order_type.lower()
    normalized_tif = time_in_force.lower()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if normalized_market not in {"US", "TW", "CRYPTO"}:
        raise ValueError("unsupported market")
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    if normalized_type not in {"market", "limit"}:
        raise ValueError("order_type must be market or limit")
    if normalized_tif not in {"day", "gtc"}:
        raise ValueError("time_in_force must be day or gtc")
    qty = _finite_positive(quantity, "quantity")
    fee = float(fee_bps)
    if not math.isfinite(fee) or fee < 0 or fee > 10_000:
        raise ValueError("fee_bps must be between 0 and 10000")
    if normalized_type == "limit":
        if limit_price is None:
            raise ValueError("limit_price is required for a limit order")
        reservation_price = _finite_positive(limit_price, "limit_price")
    else:
        if reference_price is None:
            raise ValueError("reference_price is required for a market order")
        reservation_price = _finite_positive(reference_price, "reference_price")

    existing = await db.scalar(
        select(PaperOrder).where(
            PaperOrder.portfolio_id == UUID(portfolio_id),
            PaperOrder.idempotency_key == idempotency_key,
        )
    )
    if existing:
        signature = (
            existing.symbol,
            existing.market,
            existing.side,
            existing.order_type,
            float(existing.quantity),
            float(existing.limit_price) if existing.limit_price else None,
            float(existing.reservation_price),
            float(existing.fee_bps),
            existing.time_in_force,
        )
        requested = (
            normalized_symbol,
            normalized_market,
            normalized_side,
            normalized_type,
            qty,
            float(limit_price) if limit_price is not None else None,
            reservation_price,
            fee,
            normalized_tif,
        )
        if signature != requested:
            raise PaperTradingConflict("idempotency key was already used with different order data")
        return existing

    open_orders = await _open_orders(portfolio_id, db)
    if normalized_side == "buy":
        currency = _currency(normalized_market)
        cash = await _cash_balance(portfolio_id, currency, db)
        reserved = sum(
            _buy_reservation(order)
            for order in open_orders
            if order.side == "buy" and _currency(order.market) == currency
        )
        required = qty * reservation_price * (1 + fee / 10_000)
        if required > cash - reserved + 1e-6:
            raise PaperTradingConflict(
                f"insufficient {currency} cash: available {cash - reserved:.6f}, required {required:.6f}"
            )
    else:
        holding = await db.scalar(
            select(Holding).where(
                Holding.portfolio_id == UUID(portfolio_id),
                Holding.symbol == normalized_symbol,
                Holding.market == Market[normalized_market],
            )
        )
        held = float(holding.quantity) if holding else 0.0
        reserved = sum(
            _remaining(order)
            for order in open_orders
            if order.side == "sell"
            and order.symbol == normalized_symbol
            and order.market == normalized_market
        )
        if qty > held - reserved + 1e-6:
            raise PaperTradingConflict(
                f"insufficient inventory: available {held - reserved:.6f}, required {qty:.6f}"
            )

    submitted_at = datetime.now(UTC)
    order = PaperOrder(
        portfolio_id=UUID(portfolio_id),
        symbol=normalized_symbol,
        market=normalized_market,
        side=normalized_side,
        order_type=normalized_type,
        time_in_force=normalized_tif,
        quantity=qty,
        limit_price=limit_price,
        reservation_price=reservation_price,
        fee_bps=fee,
        idempotency_key=idempotency_key,
        notes=notes,
        created_at=submitted_at,
        updated_at=submitted_at,
        expires_at=_day_expiry(normalized_market, submitted_at)
        if normalized_tif == "day"
        else None,
    )
    db.add(order)
    await db.flush()
    return order


async def get_order(
    *,
    portfolio_id: str,
    order_id: str,
    user_id: str,
    db: AsyncSession,
    lock: bool = False,
) -> PaperOrder:
    if lock:
        await _owned_portfolio_locked(portfolio_id, user_id, db)
    stmt = select(PaperOrder).where(
        PaperOrder.id == UUID(order_id),
        PaperOrder.portfolio_id == UUID(portfolio_id),
    )
    if lock:
        stmt = stmt.with_for_update()
    order = await db.scalar(stmt)
    if not order:
        raise ValueError("Paper order not found")
    if not lock:
        from services.portfolio_service import get_portfolio

        if not await get_portfolio(portfolio_id, user_id, db):
            raise ValueError("Paper order not found")
    return order


async def list_orders(
    *,
    portfolio_id: str,
    user_id: str,
    db: AsyncSession,
    limit: int = 200,
) -> list[PaperOrder]:
    from services.portfolio_service import get_portfolio

    if not await get_portfolio(portfolio_id, user_id, db):
        raise ValueError("Portfolio not found")
    return list(
        (
            await db.scalars(
                select(PaperOrder)
                .where(
                    PaperOrder.portfolio_id == UUID(portfolio_id),
                )
                .order_by(PaperOrder.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def list_fills(
    *, portfolio_id: str, order_id: str, user_id: str, db: AsyncSession
) -> list[PaperFill]:
    order = await get_order(
        portfolio_id=portfolio_id,
        order_id=order_id,
        user_id=user_id,
        db=db,
    )
    return list(
        (
            await db.scalars(
                select(PaperFill)
                .where(PaperFill.order_id == order.id)
                .order_by(PaperFill.filled_at, PaperFill.id)
            )
        ).all()
    )


async def fill_order(
    *,
    portfolio_id: str,
    order_id: str,
    user_id: str,
    quantity: float,
    price: float,
    idempotency_key: str,
    filled_at: datetime | None,
    db: AsyncSession,
) -> PaperFill:
    order = await get_order(
        portfolio_id=portfolio_id,
        order_id=order_id,
        user_id=user_id,
        db=db,
        lock=True,
    )
    existing = await db.scalar(
        select(PaperFill).where(
            PaperFill.order_id == order.id,
            PaperFill.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if float(existing.quantity) != float(quantity) or float(existing.price) != float(price):
            raise PaperTradingConflict("idempotency key was already used with different fill data")
        return existing
    if order.status not in OPEN_STATUSES:
        raise PaperTradingConflict(f"order is already {order.status}")
    qty = _finite_positive(quantity, "quantity")
    fill_price = _finite_positive(price, "price")
    remaining = _remaining(order)
    if qty > remaining + 1e-6:
        raise PaperTradingConflict(f"fill quantity exceeds remaining quantity {remaining:.6f}")
    if order.order_type == "limit":
        limit = float(order.limit_price)
        if order.side == "buy" and fill_price > limit + 1e-9:
            raise PaperTradingConflict("buy fill price exceeds limit price")
        if order.side == "sell" and fill_price < limit - 1e-9:
            raise PaperTradingConflict("sell fill price is below limit price")

    open_orders = await _open_orders(portfolio_id, db)
    if order.side == "buy":
        currency = _currency(order.market)
        cash = await _cash_balance(portfolio_id, currency, db)
        other_reserved = sum(
            _buy_reservation(item)
            for item in open_orders
            if item.id != order.id and item.side == "buy" and _currency(item.market) == currency
        )
        remaining_reserved = _buy_reservation(order, remaining - qty)
        actual = qty * fill_price * (1 + float(order.fee_bps) / 10_000)
        if actual > cash - other_reserved - remaining_reserved + 1e-6:
            raise PaperTradingConflict(f"insufficient {currency} cash at fill price")
    else:
        holding = await db.scalar(
            select(Holding).where(
                Holding.portfolio_id == UUID(portfolio_id),
                Holding.symbol == order.symbol,
                Holding.market == Market[order.market],
            )
        )
        held = float(holding.quantity) if holding else 0.0
        other_reserved = sum(
            _remaining(item)
            for item in open_orders
            if item.id != order.id
            and item.side == "sell"
            and item.symbol == order.symbol
            and item.market == order.market
        )
        if qty > held - other_reserved + 1e-6:
            raise PaperTradingConflict("insufficient inventory at fill time")

    execution_time = filled_at or datetime.now(UTC)
    fill_id = uuid.uuid4()
    from services.portfolio_service import add_transaction

    transaction = await add_transaction(
        portfolio_id=portfolio_id,
        user_id=user_id,
        symbol=order.symbol,
        market=order.market,
        tx_type=order.side,
        quantity=qty,
        price=fill_price,
        fx_rate=None,
        tx_date=execution_time.date(),
        notes=f"paper_order:{order.id}",
        db=db,
    )
    fee_amount = qty * fill_price * float(order.fee_bps) / 10_000
    if fee_amount > 1e-9:
        from services.portfolio_cash_service import append_entry

        await append_entry(
            portfolio_id=portfolio_id,
            currency=_currency(order.market),
            amount=-fee_amount,
            entry_type="fee",
            source="paper_fill",
            occurred_on=execution_time.date(),
            transaction_id=transaction.id,
            idempotency_key=f"paper-fill-fee:{fill_id}",
            metadata={"paper_order_id": str(order.id)},
            db=db,
        )
    previous_filled = float(order.filled_quantity)
    new_filled = previous_filled + qty
    previous_notional = previous_filled * float(order.average_fill_price or 0)
    order.filled_quantity = new_filled
    order.average_fill_price = (previous_notional + qty * fill_price) / new_filled
    order.status = "filled" if new_filled >= float(order.quantity) - 1e-6 else "partially_filled"
    order.updated_at = datetime.now(UTC)
    fill = PaperFill(
        id=fill_id,
        order_id=order.id,
        transaction_id=transaction.id,
        quantity=qty,
        price=fill_price,
        fee=fee_amount,
        idempotency_key=idempotency_key,
        filled_at=execution_time,
    )
    db.add(fill)
    await db.flush()
    return fill


async def cancel_order(
    *,
    portfolio_id: str,
    order_id: str,
    user_id: str,
    db: AsyncSession,
) -> PaperOrder:
    order = await get_order(
        portfolio_id=portfolio_id,
        order_id=order_id,
        user_id=user_id,
        db=db,
        lock=True,
    )
    if order.status not in OPEN_STATUSES:
        raise PaperTradingConflict(f"order is already {order.status}")
    order.status = "cancelled"
    order.cancelled_at = datetime.now(UTC)
    order.updated_at = order.cancelled_at
    await db.flush()
    return order
