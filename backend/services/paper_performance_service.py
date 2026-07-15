"""Realized paper-trading performance calculated from immutable fills."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperFill, PaperOrder
from models.portfolio import Portfolio


def _rounded(value: float) -> float:
    return round(value, 6)


async def performance(
    *, portfolio_id: str, user_id: str, db: AsyncSession, fill_limit: int
) -> dict:
    portfolio_uuid = UUID(portfolio_id)
    portfolio = await db.scalar(
        select(Portfolio).where(
            Portfolio.id == portfolio_uuid,
            Portfolio.user_id == UUID(user_id),
        )
    )
    if not portfolio:
        raise ValueError("Portfolio not found")

    total_fill_count = int(
        await db.scalar(
            select(func.count(PaperFill.id))
            .join(PaperOrder, PaperOrder.id == PaperFill.order_id)
            .where(PaperOrder.portfolio_id == portfolio_uuid)
        )
        or 0
    )
    rows = list(
        (
            await db.execute(
                select(
                    PaperFill.id,
                    PaperFill.order_id,
                    PaperFill.filled_at,
                    PaperFill.currency,
                    PaperFill.realized_pnl,
                    PaperFill.fee,
                    PaperOrder.side,
                    PaperOrder.symbol,
                    PaperOrder.market,
                )
                .join(PaperOrder, PaperOrder.id == PaperFill.order_id)
                .where(PaperOrder.portfolio_id == portfolio_uuid)
                .order_by(PaperFill.filled_at.desc(), PaperFill.id.desc())
                .limit(fill_limit)
            )
        ).all()
    )
    rows.reverse()

    summaries = {
        currency: {
            "currency": currency,
            "fill_count": 0,
            "exit_order_count": 0,
            "winning_exit_orders": 0,
            "losing_exit_orders": 0,
            "breakeven_exit_orders": 0,
            "win_rate_pct": None,
            "profit_factor": None,
            "total_realized_pnl": 0.0,
            "total_fees": 0.0,
            "best_exit_pnl": None,
            "worst_exit_pnl": None,
            "max_drawdown": 0.0,
        }
        for currency in ("USD", "TWD")
    }
    cumulative = {"USD": 0.0, "TWD": 0.0}
    peaks = {"USD": 0.0, "TWD": 0.0}
    curves: list[dict] = []
    exit_orders: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"pnl": 0.0, "symbol": "", "market": ""}
    )

    for row in rows:
        currency = row.currency if row.currency in summaries else "USD"
        pnl = float(row.realized_pnl or 0)
        fee = float(row.fee or 0)
        summary = summaries[currency]
        summary["fill_count"] += 1
        summary["total_realized_pnl"] += pnl
        summary["total_fees"] += fee
        cumulative[currency] += pnl
        peaks[currency] = max(peaks[currency], cumulative[currency])
        drawdown = cumulative[currency] - peaks[currency]
        summary["max_drawdown"] = min(summary["max_drawdown"], drawdown)
        curves.append(
            {
                "fill_id": row.id,
                "filled_at": row.filled_at,
                "currency": currency,
                "cumulative_realized_pnl": _rounded(cumulative[currency]),
                "drawdown": _rounded(drawdown),
            }
        )
        if row.side == "sell":
            exit_order = exit_orders[(currency, str(row.order_id))]
            exit_order["pnl"] += pnl
            exit_order["symbol"] = row.symbol
            exit_order["market"] = row.market

    for (currency, _), exit_order in exit_orders.items():
        pnl = exit_order["pnl"]
        summary = summaries[currency]
        summary["exit_order_count"] += 1
        if pnl > 1e-9:
            summary["winning_exit_orders"] += 1
        elif pnl < -1e-9:
            summary["losing_exit_orders"] += 1
        else:
            summary["breakeven_exit_orders"] += 1
        if summary["best_exit_pnl"] is None or pnl > summary["best_exit_pnl"]:
            summary["best_exit_pnl"] = pnl
        if summary["worst_exit_pnl"] is None or pnl < summary["worst_exit_pnl"]:
            summary["worst_exit_pnl"] = pnl

    for currency, summary in summaries.items():
        pnl_values = [
            event["pnl"] for (event_currency, _), event in exit_orders.items()
            if event_currency == currency
        ]
        decided = summary["winning_exit_orders"] + summary["losing_exit_orders"]
        if decided:
            summary["win_rate_pct"] = _rounded(summary["winning_exit_orders"] / decided * 100)
        gross_profit = sum(value for value in pnl_values if value > 0)
        gross_loss = -sum(value for value in pnl_values if value < 0)
        if gross_loss > 0:
            summary["profit_factor"] = _rounded(gross_profit / gross_loss)
        for key in (
            "total_realized_pnl",
            "total_fees",
            "best_exit_pnl",
            "worst_exit_pnl",
            "max_drawdown",
        ):
            if summary[key] is not None:
                summary[key] = _rounded(summary[key])

    return {
        "portfolio_id": portfolio_uuid,
        "window_fill_limit": fill_limit,
        "window_fill_count": len(rows),
        "total_fill_count": total_fill_count,
        "truncated": total_fill_count > len(rows),
        "window_started_at": rows[0].filled_at if rows else None,
        "window_ended_at": rows[-1].filled_at if rows else None,
        "summaries": list(summaries.values()),
        "curve": curves,
    }
