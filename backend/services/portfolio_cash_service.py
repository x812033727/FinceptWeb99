"""Append-only, owner-scoped portfolio cash ledger."""
from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.portfolio import PortfolioCashEntry, Transaction, TransactionType

CREDIT_TYPES = frozenset({"deposit", "interest", "dividend", "refund", "adjustment_credit"})
DEBIT_TYPES = frozenset({"withdrawal", "fee", "tax", "adjustment_debit"})
MANUAL_ENTRY_TYPES = CREDIT_TYPES | DEBIT_TYPES


def _currency(value: str) -> str:
    normalized = str(value).strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("currency must be a three-letter code")
    return normalized


async def _owned_portfolio(portfolio_id: str, user_id: str, db: AsyncSession):
    from services.portfolio_service import get_portfolio

    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")
    return portfolio


async def append_entry(
    *, portfolio_id: str, currency: str, amount: float, entry_type: str,
    source: str, occurred_on: date, db: AsyncSession,
    transaction_id: UUID | None = None, reversal_of: UUID | None = None,
    idempotency_key: str | None = None, notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> PortfolioCashEntry:
    parsed = float(amount)
    if not math.isfinite(parsed) or abs(parsed) < 1e-9:
        raise ValueError("cash entry amount must be finite and non-zero")
    if idempotency_key:
        existing = await db.scalar(select(PortfolioCashEntry).where(
            PortfolioCashEntry.portfolio_id == UUID(portfolio_id),
            PortfolioCashEntry.idempotency_key == idempotency_key,
        ))
        if existing:
            return existing
    entry = PortfolioCashEntry(
        portfolio_id=UUID(portfolio_id), currency=_currency(currency), amount=parsed,
        entry_type=entry_type, source=source, occurred_on=occurred_on,
        transaction_id=transaction_id, reversal_of=reversal_of,
        idempotency_key=idempotency_key, notes=notes,
        entry_metadata=metadata,
    )
    db.add(entry)
    await db.flush()
    return entry


async def create_manual_entry(
    *, portfolio_id: str, user_id: str, currency: str, amount: float,
    entry_type: str, occurred_on: date, notes: str | None,
    idempotency_key: str | None, db: AsyncSession,
) -> PortfolioCashEntry:
    await _owned_portfolio(portfolio_id, user_id, db)
    normalized_type = str(entry_type).strip().lower()
    if normalized_type not in MANUAL_ENTRY_TYPES:
        raise ValueError("unsupported manual cash entry type")
    magnitude = float(amount)
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise ValueError("amount must be finite and positive")
    signed = magnitude if normalized_type in CREDIT_TYPES else -magnitude
    return await append_entry(
        portfolio_id=portfolio_id, currency=currency, amount=signed,
        entry_type=normalized_type, source="manual", occurred_on=occurred_on,
        idempotency_key=idempotency_key, notes=notes, db=db,
    )


async def list_entries(
    *, portfolio_id: str, user_id: str, db: AsyncSession,
    limit: int = 200,
) -> list[dict[str, Any]]:
    await _owned_portfolio(portfolio_id, user_id, db)
    rows = await db.scalars(
        select(PortfolioCashEntry).where(
            PortfolioCashEntry.portfolio_id == UUID(portfolio_id),
        ).order_by(
            PortfolioCashEntry.occurred_on.desc(),
            PortfolioCashEntry.created_at.desc(),
        ).limit(limit)
    )
    entries = list(rows)
    entry_ids = [entry.id for entry in entries]
    reversed_ids = set((await db.scalars(select(PortfolioCashEntry.reversal_of).where(
        PortfolioCashEntry.reversal_of.in_(entry_ids),
    ))).all()) if entry_ids else set()
    return [{
        "id": entry.id, "portfolio_id": entry.portfolio_id,
        "currency": entry.currency, "amount": float(entry.amount),
        "entry_type": entry.entry_type, "source": entry.source,
        "occurred_on": entry.occurred_on, "transaction_id": entry.transaction_id,
        "reversal_of": entry.reversal_of, "idempotency_key": entry.idempotency_key,
        "notes": entry.notes, "entry_metadata": entry.entry_metadata,
        "created_at": entry.created_at, "is_reversed": entry.id in reversed_ids,
    } for entry in entries]


async def get_cash_balances(
    *, portfolio_id: str, user_id: str, db: AsyncSession,
    as_of: date | None = None,
) -> dict[str, float]:
    await _owned_portfolio(portfolio_id, user_id, db)
    stmt = select(
        PortfolioCashEntry.currency, func.sum(PortfolioCashEntry.amount),
    ).where(PortfolioCashEntry.portfolio_id == UUID(portfolio_id))
    if as_of is not None:
        stmt = stmt.where(PortfolioCashEntry.occurred_on <= as_of)
    rows = (await db.execute(stmt.group_by(PortfolioCashEntry.currency))).all()
    return {
        str(currency): round(float(amount or 0), 6)
        for currency, amount in rows
    }


async def cash_value_in_currency(
    *, balances: dict[str, float], target_currency: str,
) -> float:
    from services.portfolio_service import _to_portfolio_currency

    return sum([
        await _to_portfolio_currency(amount, currency, target_currency)
        for currency, amount in balances.items()
    ])


async def reverse_entry(
    *, portfolio_id: str, entry_id: str, user_id: str,
    db: AsyncSession, notes: str | None = None,
) -> PortfolioCashEntry:
    await _owned_portfolio(portfolio_id, user_id, db)
    entry = await db.get(PortfolioCashEntry, UUID(entry_id))
    if not entry or str(entry.portfolio_id) != portfolio_id:
        raise ValueError("Cash entry not found")
    if entry.entry_type == "reversal":
        raise ValueError("A reversal cannot be reversed")
    if entry.transaction_id is not None:
        raise ValueError("Transaction settlements must be corrected through the transaction")
    existing = await db.scalar(select(PortfolioCashEntry).where(
        PortfolioCashEntry.reversal_of == entry.id,
    ))
    if existing:
        raise ValueError("Cash entry is already reversed")
    return await append_entry(
        portfolio_id=portfolio_id, currency=entry.currency,
        amount=-float(entry.amount), entry_type="reversal", source="manual_reversal",
        occurred_on=date.today(), reversal_of=entry.id,
        idempotency_key=f"reversal:{entry.id}", notes=notes,
        metadata={"original_entry_type": entry.entry_type}, db=db,
    )


def transaction_cash_amount(transaction: Transaction) -> float:
    gross = float(transaction.quantity) * float(transaction.price)
    return -gross if transaction.tx_type == TransactionType.buy else gross


def transaction_settlement_currency(transaction: Transaction) -> str:
    return "TWD" if transaction.market.value == "TW" else "USD"


async def replace_transaction_settlement(
    *, transaction: Transaction, db: AsyncSession, reason: str,
) -> PortfolioCashEntry | None:
    active = list((await db.scalars(select(PortfolioCashEntry).where(
        PortfolioCashEntry.transaction_id == transaction.id,
        PortfolioCashEntry.entry_type == "trade_settlement",
    ))).all())
    for original in active:
        already_reversed = await db.scalar(select(PortfolioCashEntry.id).where(
            PortfolioCashEntry.reversal_of == original.id,
        ))
        if already_reversed:
            continue
        await append_entry(
            portfolio_id=str(transaction.portfolio_id), currency=original.currency,
            amount=-float(original.amount), entry_type="reversal",
            source="transaction_reversal", occurred_on=original.occurred_on,
            transaction_id=transaction.id, reversal_of=original.id,
            idempotency_key=f"transaction-reversal:{original.id}",
            metadata={"reason": reason}, db=db,
        )
    amount = transaction_cash_amount(transaction)
    if abs(amount) < 1e-9:
        return None
    return await append_entry(
        portfolio_id=str(transaction.portfolio_id),
        currency=transaction_settlement_currency(transaction),
        amount=amount, entry_type="trade_settlement", source="transaction",
        occurred_on=transaction.tx_date, transaction_id=transaction.id,
        idempotency_key=f"transaction:{transaction.id}:{uuid.uuid4()}",
        metadata={
            "tx_type": transaction.tx_type.value, "reason": reason,
            "portfolio_fx_rate": float(transaction.fx_rate or 1),
        }, db=db,
    )


async def reverse_transaction_settlement(
    *, transaction: Transaction, db: AsyncSession, reason: str,
) -> None:
    active = list((await db.scalars(select(PortfolioCashEntry).where(
        PortfolioCashEntry.transaction_id == transaction.id,
        PortfolioCashEntry.entry_type == "trade_settlement",
    ))).all())
    for original in active:
        already_reversed = await db.scalar(select(PortfolioCashEntry.id).where(
            PortfolioCashEntry.reversal_of == original.id,
        ))
        if already_reversed:
            continue
        await append_entry(
            portfolio_id=str(transaction.portfolio_id), currency=original.currency,
            amount=-float(original.amount), entry_type="reversal",
            source="transaction_reversal", occurred_on=original.occurred_on,
            transaction_id=transaction.id, reversal_of=original.id,
            idempotency_key=f"transaction-reversal:{original.id}",
            metadata={"reason": reason}, db=db,
        )
