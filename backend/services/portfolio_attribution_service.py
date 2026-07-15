"""Transaction-aware portfolio return attribution.

This is deliberately *not* labelled Brinson attribution: the application does
not yet store point-in-time benchmark constituent/sector weights.  Instead we
use Modified Dietz, which is well-defined with the data Fincept already owns:
dated cash flows, quantities, historical prices and trade-day FX rates.

Security trades are flows between the security and its native-currency cash
sleeve. Manual deposits/withdrawals are external flows. Including both sleeves
makes internal settlements cancel while dividends and FX on cash remain in
portfolio return.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from models.portfolio import Holding, PortfolioCashEntry, Transaction
from services.crypto_market_service import get_history as crypto_history
from services.tw_market_service import get_history as tw_history
from services.us_market_service import get_history as us_history

log = logging.getLogger(__name__)

METHOD_VERSION = "modified-dietz-cash-ledger-v2"
ALLOWED_DAYS = (30, 90, 180, 365)


def _market_value_flow(tx: Transaction) -> float:
    amount = float(tx.quantity) * float(tx.price) * float(tx.fx_rate or 1.0)
    kind = tx.tx_type.value if hasattr(tx.tx_type, "value") else str(tx.tx_type)
    return amount if kind == "buy" else -amount


def _quantity_delta(tx: Transaction) -> float:
    kind = tx.tx_type.value if hasattr(tx.tx_type, "value") else str(tx.tx_type)
    if kind == "buy":
        return float(tx.quantity)
    if kind == "sell":
        return -float(tx.quantity)
    return 0.0


def _bar_date(bar: dict[str, Any]) -> date | None:
    raw = bar.get("date") or bar.get("time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None


async def _history(symbol: str, market: str, days: int) -> list[dict[str, Any]]:
    if market == "US":
        # Fetch one window wider than requested so a weekend/holiday period
        # boundary still has a genuine prior close (not a post-boundary proxy).
        period = "3mo" if days == 30 else "6mo" if days == 90 else "1y" if days == 180 else "2y"
        return await us_history(symbol, period=period, interval="1d")
    if market == "CRYPTO":
        return await crypto_history(symbol, interval="1d", limit=days + 10)
    return await tw_history(symbol, months=max(2, (days + 29) // 30 + 1))


async def _boundary_prices(
    symbol: str, market: str, start: date, end: date, days: int,
) -> tuple[date, float, date, float] | None:
    try:
        rows: list[tuple[date, float]] = []
        for bar in await _history(symbol, market, days):
            day = _bar_date(bar)
            close = bar.get("close")
            if day is not None and close is not None and float(close) > 0 and day <= end:
                rows.append((day, float(close)))
        rows.sort(key=lambda row: row[0])
        if len(rows) < 2:
            return None
        before = [row for row in rows if row[0] <= start]
        if not before:
            return None
        start_row = before[-1]
        after = [row for row in rows if row[0] >= start_row[0]]
        end_row = after[-1]
        if end_row[0] <= start_row[0]:
            return None
        return start_row[0], start_row[1], end_row[0], end_row[1]
    except Exception:
        log.warning(
            "portfolio_attribution.history_failed",
            extra={"symbol": symbol, "market": market},
            exc_info=True,
        )
        return None


async def _cash_value(
    amount: float, currency: str, portfolio_currency: str, on_date: date,
) -> float:
    from services.portfolio_service import (
        _get_historical_twd_usd,
        _get_twd_usd_rate,
        _normalize_currency,
    )

    source = _normalize_currency(currency)
    target = _normalize_currency(portfolio_currency)
    if source == target:
        return amount
    rate = await _get_historical_twd_usd(on_date)
    if rate is None:
        rate = await _get_twd_usd_rate()
    if source == "TWD" and target == "USD":
        return amount / rate
    if source == "USD" and target == "TWD":
        return amount * rate
    return amount


async def get_portfolio_attribution(
    portfolio_id: str, user_id: str, db, *, days: int = 90,
) -> dict[str, Any]:
    if days not in ALLOWED_DAYS:
        raise ValueError(f"days must be one of {ALLOWED_DAYS}")

    from services.portfolio_service import get_default_fx_rate, get_portfolio

    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")

    end = date.today()
    start = end - timedelta(days=days)
    txs = list((await db.scalars(
        select(Transaction).where(
            Transaction.portfolio_id == UUID(portfolio_id),
            Transaction.tx_date <= end,
        ).order_by(Transaction.tx_date, Transaction.created_at)
    )).all())
    holdings = list((await db.scalars(
        select(Holding).where(Holding.portfolio_id == UUID(portfolio_id))
    )).all())
    cash_entries = list((await db.scalars(
        select(PortfolioCashEntry).where(
            PortfolioCashEntry.portfolio_id == UUID(portfolio_id),
            PortfolioCashEntry.occurred_on <= end,
        ).order_by(PortfolioCashEntry.occurred_on, PortfolioCashEntry.created_at)
    )).all())

    keys = {(tx.market.value, tx.symbol) for tx in txs}
    keys.update((h.market.value, h.symbol) for h in holdings)
    if not keys and not cash_entries:
        return {
            "portfolio_id": portfolio_id, "currency": portfolio.currency,
            "methodology_version": METHOD_VERSION, "requested_days": days,
            "period_start": start, "period_end": end, "empty": True,
            "portfolio_return_pct": None, "benchmark": None,
            "benchmark_return_pct": None, "active_return_pct": None,
            "denominator": 0.0, "markets": [], "positions": [], "excluded": [],
            "disclaimer": "Return attribution is unavailable until the portfolio has transactions.",
        }

    ordered_keys = sorted(keys)
    histories = await asyncio.gather(*[
        _boundary_prices(symbol, market, start, end, days)
        for market, symbol in ordered_keys
    ])
    benchmark_symbol, benchmark_market = ("_TAIEX_TR", "TW") if portfolio.currency == "TWD" else ("SPY", "US")
    benchmark_history = await _boundary_prices(benchmark_symbol, benchmark_market, start, end, days)

    total_days = max((end - start).days, 1)
    drafts: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for (market, symbol), boundaries in zip(ordered_keys, histories):
        if boundaries is None:
            excluded.append({"symbol": symbol, "market": market, "reason": "insufficient_history"})
            continue
        start_day, start_price, end_day, end_price = boundaries
        symbol_txs = [tx for tx in txs if tx.symbol == symbol and tx.market.value == market]
        start_qty = sum(_quantity_delta(tx) for tx in symbol_txs if tx.tx_date <= start)
        end_qty = sum(_quantity_delta(tx) for tx in symbol_txs if tx.tx_date <= end)
        period_txs = [tx for tx in symbol_txs if start < tx.tx_date <= end]

        start_fx, end_fx = await asyncio.gather(
            get_default_fx_rate(market, portfolio.currency, start_day),
            get_default_fx_rate(market, portfolio.currency, end_day),
        )
        start_value = start_qty * start_price * start_fx
        end_value = end_qty * end_price * end_fx
        cash_flow = sum(_market_value_flow(tx) for tx in period_txs)
        weighted_flow = sum(
            _market_value_flow(tx) * max((end - tx.tx_date).days, 0) / total_days
            for tx in period_txs
        )
        numerator = end_value - start_value - cash_flow
        denominator = start_value + weighted_flow
        drafts.append({
            "symbol": symbol, "market": market,
            "start_date": start_day, "end_date": end_day,
            "start_quantity": round(start_qty, 6), "end_quantity": round(end_qty, 6),
            "start_value": round(start_value, 2), "end_value": round(end_value, 2),
            "net_cash_flow": round(cash_flow, 2), "weighted_cash_flow": round(weighted_flow, 2),
            "pnl_after_flows": round(numerator, 2), "denominator": denominator,
            "position_return_pct": round(numerator / denominator * 100, 4) if denominator > 0 else None,
        })

    for currency in sorted({entry.currency for entry in cash_entries}):
        currency_entries = [entry for entry in cash_entries if entry.currency == currency]
        start_native = sum(
            float(entry.amount) for entry in currency_entries
            if entry.occurred_on <= start
        )
        end_native = sum(float(entry.amount) for entry in currency_entries)
        period_entries = [
            entry for entry in currency_entries if start < entry.occurred_on <= end
        ]
        start_value, end_value = await asyncio.gather(
            _cash_value(start_native, currency, portfolio.currency, start),
            _cash_value(end_native, currency, portfolio.currency, end),
        )
        converted_flows = await asyncio.gather(*[
            _cash_value(
                float(entry.amount), currency, portfolio.currency, entry.occurred_on,
            )
            for entry in period_entries
        ])
        cash_flow = sum(converted_flows)
        weighted_flow = sum(
            value * max((end - entry.occurred_on).days, 0) / total_days
            for entry, value in zip(period_entries, converted_flows)
        )
        numerator = end_value - start_value - cash_flow
        cash_denominator = start_value + weighted_flow
        drafts.append({
            "symbol": f"CASH:{currency}", "market": "CASH",
            "start_date": start, "end_date": end,
            "start_quantity": round(start_native, 6),
            "end_quantity": round(end_native, 6),
            "start_value": round(start_value, 2), "end_value": round(end_value, 2),
            "net_cash_flow": round(cash_flow, 2),
            "weighted_cash_flow": round(weighted_flow, 2),
            "pnl_after_flows": round(numerator, 2),
            "denominator": cash_denominator,
            "position_return_pct": (
                round(numerator / cash_denominator * 100, 4)
                if cash_denominator > 0 else None
            ),
        })

    denominator = sum(row["denominator"] for row in drafts)
    numerator = sum(row["pnl_after_flows"] for row in drafts)
    portfolio_return = numerator / denominator * 100 if denominator > 0 else None
    positions = []
    for row in drafts:
        row["start_weight_pct"] = round(row["denominator"] / denominator * 100, 4) if denominator > 0 else None
        row["contribution_pct"] = round(row["pnl_after_flows"] / denominator * 100, 4) if denominator > 0 else None
        row["denominator"] = round(row["denominator"], 2)
        positions.append(row)
    positions.sort(key=lambda row: abs(row["contribution_pct"] or 0), reverse=True)
    markets = []
    for market in sorted({row["market"] for row in drafts}):
        rows = [row for row in drafts if row["market"] == market]
        market_denominator = sum(row["denominator"] for row in rows)
        market_pnl = sum(row["pnl_after_flows"] for row in rows)
        markets.append({
            "market": market,
            "start_weight_pct": round(market_denominator / denominator * 100, 4) if denominator > 0 else None,
            "market_return_pct": round(market_pnl / market_denominator * 100, 4) if market_denominator > 0 else None,
            "contribution_pct": round(market_pnl / denominator * 100, 4) if denominator > 0 else None,
            "pnl_after_flows": round(market_pnl, 2),
        })
    markets.sort(key=lambda row: abs(row["contribution_pct"] or 0), reverse=True)

    benchmark_return = None
    if benchmark_history is not None:
        _, benchmark_start, _, benchmark_end = benchmark_history
        benchmark_return = (benchmark_end / benchmark_start - 1) * 100
    return {
        "portfolio_id": portfolio_id, "currency": portfolio.currency,
        "methodology_version": METHOD_VERSION, "requested_days": days,
        "period_start": start, "period_end": end, "empty": not positions,
        "portfolio_return_pct": round(portfolio_return, 4) if portfolio_return is not None else None,
        "benchmark": benchmark_symbol if benchmark_return is not None else None,
        "benchmark_return_pct": round(benchmark_return, 4) if benchmark_return is not None else None,
        "active_return_pct": round(portfolio_return - benchmark_return, 4)
        if portfolio_return is not None and benchmark_return is not None else None,
        "denominator": round(denominator, 2), "markets": markets,
        "positions": positions, "excluded": excluded,
        "disclaimer": (
            "Modified Dietz includes native-currency cash sleeves. Security settlements "
            "cancel as internal flows; manual funding is treated as external and dividends "
            "remain investment return. It is not Brinson sector attribution; benchmark "
            "constituent history is not yet available."
        ),
    }
