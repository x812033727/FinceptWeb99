"""Portfolio-scoped risk controls for paper orders and fills."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperFill, PaperOrder, PaperRiskPolicy
from models.portfolio import Holding, Portfolio, PortfolioCashEntry


class PaperRiskViolation(ValueError):
    """A paper order or fill exceeds its portfolio risk policy."""


POLICY_FIELDS = (
    "max_order_notional_usd",
    "max_order_notional_twd",
    "max_position_notional_usd",
    "max_position_notional_twd",
    "max_daily_loss_usd",
    "max_daily_loss_twd",
    "max_open_orders",
    "max_symbol_concentration_pct",
)


def _currency(market: str) -> str:
    return "TWD" if market == "TW" else "USD"


async def _owned_portfolio(
    portfolio_id: str, user_id: str, db: AsyncSession, *, lock: bool = False
) -> Portfolio:
    stmt = select(Portfolio).where(
        Portfolio.id == UUID(portfolio_id), Portfolio.user_id == UUID(user_id)
    )
    if lock:
        stmt = stmt.with_for_update()
    portfolio = await db.scalar(stmt)
    if not portfolio:
        raise ValueError("Portfolio not found")
    return portfolio


async def daily_realized_pnl(
    portfolio_id: str, db: AsyncSession, *, now: datetime | None = None
) -> dict[str, float]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    day_start = datetime.combine(current.date(), time(), UTC)
    rows = (
        await db.execute(
            select(PaperFill.currency, func.sum(PaperFill.realized_pnl))
            .join(PaperOrder, PaperOrder.id == PaperFill.order_id)
            .where(
                PaperOrder.portfolio_id == UUID(portfolio_id),
                PaperFill.filled_at >= day_start,
            )
            .group_by(PaperFill.currency)
        )
    ).all()
    totals = {"USD": 0.0, "TWD": 0.0}
    for currency, amount in rows:
        if currency in totals:
            totals[currency] = float(amount or 0)
    return totals


async def get_policy_state(*, portfolio_id: str, user_id: str, db: AsyncSession) -> dict:
    await _owned_portfolio(portfolio_id, user_id, db)
    policy = await db.get(PaperRiskPolicy, UUID(portfolio_id))
    pnl = await daily_realized_pnl(portfolio_id, db)
    state = {
        "portfolio_id": UUID(portfolio_id),
        "configured": policy is not None,
        "trading_enabled": bool(policy.trading_enabled) if policy else True,
        "updated_at": policy.updated_at if policy else None,
        "cancelled_open_orders": 0,
        "daily_realized_pnl_usd": pnl["USD"],
        "daily_realized_pnl_twd": pnl["TWD"],
    }
    for field in POLICY_FIELDS:
        value = getattr(policy, field) if policy else None
        state[field] = float(value) if value is not None else None
    if policy and policy.max_open_orders is not None:
        state["max_open_orders"] = int(policy.max_open_orders)
    return state


async def update_policy(
    *,
    portfolio_id: str,
    user_id: str,
    trading_enabled: bool,
    db: AsyncSession,
    **limits,
) -> dict:
    await _owned_portfolio(portfolio_id, user_id, db, lock=True)
    policy = await db.scalar(
        select(PaperRiskPolicy)
        .where(PaperRiskPolicy.portfolio_id == UUID(portfolio_id))
        .with_for_update()
    )
    if policy is None:
        policy = PaperRiskPolicy(portfolio_id=UUID(portfolio_id))
        db.add(policy)
    policy.trading_enabled = trading_enabled
    for field in POLICY_FIELDS:
        setattr(policy, field, limits.get(field))
    now = datetime.now(UTC)
    policy.updated_at = now

    cancelled = 0
    if not trading_enabled:
        orders = list(
            (
                await db.scalars(
                    select(PaperOrder)
                    .where(
                        PaperOrder.portfolio_id == UUID(portfolio_id),
                        PaperOrder.status.in_(("pending", "partially_filled")),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for order in orders:
            order.status = "cancelled"
            order.cancelled_at = now
            order.updated_at = now
        cancelled = len(orders)
    await db.flush()
    state = await get_policy_state(portfolio_id=portfolio_id, user_id=user_id, db=db)
    state["cancelled_open_orders"] = cancelled
    return state


def _limit(policy: PaperRiskPolicy, prefix: str, currency: str) -> float | None:
    value = getattr(policy, f"{prefix}_{currency.lower()}")
    return float(value) if value is not None else None


async def _enforce_common(
    policy: PaperRiskPolicy, portfolio_id: str, currency: str, db: AsyncSession
) -> None:
    if not policy.trading_enabled:
        raise PaperRiskViolation("paper trading kill switch is engaged")
    daily_limit = _limit(policy, "max_daily_loss", currency)
    if daily_limit is not None:
        pnl = await daily_realized_pnl(portfolio_id, db)
        loss = max(0.0, -pnl[currency])
        if loss + 1e-9 >= daily_limit:
            raise PaperRiskViolation(
                f"daily {currency} realized loss limit reached: {loss:.6f} / {daily_limit:.6f}"
            )


async def _cost_exposure(
    portfolio_id: str, currency: str, db: AsyncSession
) -> tuple[float, dict[tuple[str, str], float]]:
    holdings = list(
        (
            await db.scalars(
                select(Holding).where(
                    Holding.portfolio_id == UUID(portfolio_id),
                    Holding.cost_currency == currency,
                )
            )
        ).all()
    )
    by_symbol: dict[tuple[str, str], float] = {}
    gross = 0.0
    for holding in holdings:
        exposure = float(holding.quantity) * float(holding.avg_cost)
        key = (str(holding.market.value), holding.symbol)
        by_symbol[key] = by_symbol.get(key, 0.0) + exposure
        gross += exposure
    cash = await db.scalar(
        select(func.sum(PortfolioCashEntry.amount)).where(
            PortfolioCashEntry.portfolio_id == UUID(portfolio_id),
            PortfolioCashEntry.currency == currency,
        )
    )
    return max(0.0, gross + float(cash or 0)), by_symbol


def _pending_buy_exposure(
    open_orders: list[PaperOrder],
    currency: str,
    *,
    current_order_id=None,
    current_fill_quantity: float = 0,
) -> tuple[float, dict[tuple[str, str], float]]:
    gross = 0.0
    by_symbol: dict[tuple[str, str], float] = {}
    for order in open_orders:
        if order.side != "buy" or _currency(order.market) != currency:
            continue
        remaining = max(0.0, float(order.quantity) - float(order.filled_quantity or 0))
        if current_order_id is not None and order.id == current_order_id:
            remaining = max(0.0, remaining - current_fill_quantity)
        exposure = remaining * float(order.reservation_price)
        key = (order.market, order.symbol)
        by_symbol[key] = by_symbol.get(key, 0.0) + exposure
        gross += exposure
    return gross, by_symbol


async def _enforce_buy_exposure(
    *,
    policy: PaperRiskPolicy,
    portfolio_id: str,
    market: str,
    symbol: str,
    additional_notional: float,
    open_orders: list[PaperOrder],
    db: AsyncSession,
    current_order_id=None,
    current_fill_quantity: float = 0,
) -> None:
    currency = _currency(market)
    capital, holdings = await _cost_exposure(portfolio_id, currency, db)
    _, pending = _pending_buy_exposure(
        open_orders,
        currency,
        current_order_id=current_order_id,
        current_fill_quantity=current_fill_quantity,
    )
    key = (market, symbol)
    projected_symbol = holdings.get(key, 0.0) + pending.get(key, 0.0) + additional_notional
    position_limit = _limit(policy, "max_position_notional", currency)
    if position_limit is not None and projected_symbol > position_limit + 1e-6:
        raise PaperRiskViolation(
            f"projected {symbol} {currency} exposure {projected_symbol:.6f} "
            f"exceeds position limit {position_limit:.6f}"
        )
    concentration_limit = (
        float(policy.max_symbol_concentration_pct)
        if policy.max_symbol_concentration_pct is not None
        else None
    )
    if concentration_limit is not None:
        concentration = projected_symbol / capital * 100 if capital > 0 else float("inf")
        if concentration > concentration_limit + 1e-9:
            raise PaperRiskViolation(
                f"projected {symbol} concentration {concentration:.4f}% "
                f"exceeds limit {concentration_limit:.4f}%"
            )


async def enforce_submission(
    *,
    portfolio_id: str,
    market: str,
    symbol: str,
    side: str,
    quantity: float,
    reservation_price: float,
    open_orders: list[PaperOrder],
    db: AsyncSession,
) -> None:
    policy = await db.get(PaperRiskPolicy, UUID(portfolio_id))
    if policy is None:
        return
    currency = _currency(market)
    await _enforce_common(policy, portfolio_id, currency, db)
    if policy.max_open_orders is not None and len(open_orders) >= int(policy.max_open_orders):
        raise PaperRiskViolation(
            f"open paper order limit reached: {len(open_orders)} / {int(policy.max_open_orders)}"
        )
    notional = quantity * reservation_price
    order_limit = _limit(policy, "max_order_notional", currency)
    if order_limit is not None and notional > order_limit + 1e-6:
        raise PaperRiskViolation(
            f"order notional {notional:.6f} {currency} exceeds limit {order_limit:.6f}"
        )
    if side == "buy":
        await _enforce_buy_exposure(
            policy=policy,
            portfolio_id=portfolio_id,
            market=market,
            symbol=symbol,
            additional_notional=notional,
            open_orders=open_orders,
            db=db,
        )


async def enforce_fill(
    *,
    portfolio_id: str,
    order: PaperOrder,
    quantity: float,
    price: float,
    open_orders: list[PaperOrder],
    db: AsyncSession,
) -> None:
    policy = await db.get(PaperRiskPolicy, UUID(portfolio_id))
    if policy is None:
        return
    currency = _currency(order.market)
    await _enforce_common(policy, portfolio_id, currency, db)
    if order.side == "buy":
        await _enforce_buy_exposure(
            policy=policy,
            portfolio_id=portfolio_id,
            market=order.market,
            symbol=order.symbol,
            additional_notional=quantity * price,
            open_orders=open_orders,
            db=db,
            current_order_id=order.id,
            current_fill_quantity=quantity,
        )
