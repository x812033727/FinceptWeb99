"""
Portfolio service — CRUD, P&L calculation, multi-currency, optimisation.

Multi-currency rule:
  avg_cost and price are stored in cost_currency (TWD for TW stocks, USD for US).
  P&L is computed in cost_currency, then converted to portfolio.currency
  using the TWD/USD FX rate from FRED (series DEXTW).
  FX rate is cached 4h in Redis.
"""
import asyncio
import hashlib
import json
import logging
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.cache_ttls import (
    TTL_FX,
    TTL_FX_HISTORICAL,
    TTL_FX_LAST_KNOWN,
)
from cache.redis_cache import cache_get, cache_set
from data.us.fred_connector import get_latest
from models.portfolio import (
    Holding,
    Portfolio,
    PortfolioSnapshot,
    PortfolioTransactionImport,
    Transaction,
    TransactionType,
)
from services.crypto_market_service import get_quote as crypto_quote
from services.tw_market_service import get_quote as tw_quote
from services.us_market_service import get_quote as us_quote

logger = logging.getLogger(__name__)

_FX_HARD_FALLBACK = 32.0   # only used on cold cache + FRED failure


# ── FX helpers ────────────────────────────────────────────────────

async def _get_twd_usd_rate() -> float:
    """
    TWD per 1 USD. Fresh cache 4h from FRED DEXTW; falls back to the
    last-known-good rate (30d TTL) if FRED is down. Hard-coded 32.0 is
    only used if the long-term cache is also empty (first cold start).
    """
    key = "fx:twd_usd"
    key_last = "fx:twd_usd:last_known"

    cached = await cache_get(key)
    if cached:
        return float(cached)

    rate = await get_latest("DEXTW")
    if rate:
        await cache_set(key, str(rate), TTL_FX)
        await cache_set(key_last, str(rate), TTL_FX_LAST_KNOWN)
        return rate

    last_known = await cache_get(key_last)
    if last_known:
        logger.warning(
            "FRED DEXTW unavailable; using last-known TWD/USD rate %s", last_known,
        )
        return float(last_known)

    logger.error(
        "FRED DEXTW unavailable and no cached rate; using hard fallback %.2f",
        _FX_HARD_FALLBACK,
    )
    return _FX_HARD_FALLBACK


async def _get_historical_twd_usd(d: date) -> float | None:
    """
    TWD per 1 USD on the given date. Hits FRED DEXTW with a small window
    around the date and returns the last observation on or before `d`
    (markets closed weekends/holidays so we walk back). Cached forever
    keyed by date (the historical rate doesn't change once published).

    Returns None if FRED is unreachable or has no observations yet.
    """
    key = f"fx:twd_usd:hist:{d.isoformat()}"
    cached = await cache_get(key)
    if cached:
        try:
            return float(cached)
        except ValueError:
            pass

    # Look back 14 days so we always cross a real trading day even after
    # long weekends or holidays.
    start = (d - timedelta(days=14)).isoformat()
    end = d.isoformat()
    try:
        from data.us.fred_connector import get_series
        obs = await get_series("DEXTW", start_date=start, end_date=end)
    except Exception:
        obs = []

    rate: float | None = None
    for row in reversed(obs):
        if row.get("value") is not None:
            rate = float(row["value"])
            break

    if rate is not None:
        await cache_set(key, str(rate), TTL_FX_HISTORICAL)
    return rate


# Stablecoins peg-to-USD — treat as USD for portfolio valuation purposes.
# Users entering USDT cost basis effectively means USD; we don't dynamically
# track depeg events (that's a niche feature for stablecoin traders).
_USD_STABLE_EQUIVALENTS = frozenset({"USD", "USDT", "USDC", "DAI", "BUSD", "TUSD"})


def _normalize_currency(c: str) -> str:
    """Map stablecoin tickers to their USD peg for FX conversion."""
    c = c.upper()
    return "USD" if c in _USD_STABLE_EQUIVALENTS else c


async def _to_portfolio_currency(amount: float, cost_currency: str, portfolio_currency: str) -> float:
    """Convert amount from cost_currency to portfolio_currency.

    Stablecoins (USDT/USDC/DAI/BUSD/TUSD) are treated as USD. Supported
    real conversions: TWD ↔ USD via FRED DEXTW. Unsupported pairs return
    the amount unchanged (logged at higher layers if needed).
    """
    cost = _normalize_currency(cost_currency)
    port = _normalize_currency(portfolio_currency)
    if cost == port:
        return amount
    twd_usd = await _get_twd_usd_rate()
    if cost == "TWD" and port == "USD":
        return amount / twd_usd
    if cost == "USD" and port == "TWD":
        return amount * twd_usd
    return amount   # unsupported pair — return as-is


def _market_currency(market: str) -> str:
    """Native trading currency for a given market."""
    m = market.upper()
    return "TWD" if m == "TW" else "USD"   # US + CRYPTO both quoted in USD


async def get_default_fx_rate(market: str, portfolio_currency: str, tx_date: date) -> float:
    """
    Return the per-unit fx_rate to apply on a transaction so that
    `price * quantity * fx_rate = cost in portfolio_currency`.

      same currency       → 1.0 (no conversion)
      USD trade, TWD port → historical TWD/USD on tx_date (≈ 32)
      TWD trade, USD port → 1 / (historical TWD/USD on tx_date)
      unsupported pair    → 1.0 (caller can override manually)

    Falls through to the current rate if the historical lookup fails
    (FRED outage, missing API key, very recent date with no obs yet).
    """
    src = _normalize_currency(_market_currency(market))
    dst = _normalize_currency(portfolio_currency)
    if src == dst:
        return 1.0

    rate = await _get_historical_twd_usd(tx_date)
    if rate is None:
        rate = await _get_twd_usd_rate()
    if not rate or rate <= 0:
        return 1.0

    if src == "USD" and dst == "TWD":
        return rate
    if src == "TWD" and dst == "USD":
        return 1.0 / rate
    return 1.0


# ── CRUD ──────────────────────────────────────────────────────────

async def list_portfolios(user_id: str, db: AsyncSession) -> list[Portfolio]:
    rows = await db.scalars(
        select(Portfolio).where(Portfolio.user_id == UUID(user_id)).order_by(Portfolio.created_at)
    )
    return list(rows)


async def create_portfolio(user_id: str, name: str, currency: str, db: AsyncSession) -> Portfolio:
    p = Portfolio(user_id=UUID(user_id), name=name, currency=currency.upper())
    db.add(p)
    await db.flush()
    return p


async def get_portfolio(portfolio_id: str, user_id: str, db: AsyncSession) -> Portfolio | None:
    p = await db.get(Portfolio, UUID(portfolio_id))
    if not p or str(p.user_id) != user_id:
        return None
    return p


async def _lock_owned_portfolio(
    portfolio_id: str, user_id: str, db: AsyncSession,
) -> Portfolio | None:
    """Serialize transaction mutations within one owned portfolio."""
    return await db.scalar(
        select(Portfolio)
        .where(
            Portfolio.id == UUID(portfolio_id),
            Portfolio.user_id == UUID(user_id),
        )
        .with_for_update()
    )


async def delete_portfolio(portfolio_id: str, user_id: str, db: AsyncSession) -> bool:
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        return False
    await db.delete(p)
    return True


async def update_portfolio(
    portfolio_id: str, user_id: str, db: AsyncSession,
    *,
    name: str | None = None,
    currency: str | None = None,
) -> Portfolio | None:
    """Rename a portfolio and/or change its base currency.

    Changing currency does NOT retroactively re-stamp historical snapshots —
    those keep the currency they were taken in. Going forward the new
    currency drives FX conversion in get_portfolio_detail.
    """
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        return None
    if name is not None:
        p.name = name.strip() or p.name
    if currency is not None:
        p.currency = currency.upper()
    await db.flush()
    return p


# ── Transactions ──────────────────────────────────────────────────

async def add_transaction(
    portfolio_id: str,
    user_id: str,
    symbol: str,
    market: str,
    tx_type: str,
    quantity: float,
    price: float,
    fx_rate: float | None,
    tx_date: date,
    notes: str | None,
    db: AsyncSession,
) -> Transaction:
    p = await _lock_owned_portfolio(portfolio_id, user_id, db)
    if not p:
        raise ValueError("Portfolio not found")

    # Auto-stamp the trade-day FX rate when the caller didn't pin one.
    # `0` is treated the same as `None` (the legacy default in older
    # forms) so historical converts even from those.
    if fx_rate is None or fx_rate == 0:
        fx_rate = await get_default_fx_rate(market, p.currency, tx_date)

    from models.portfolio import Market as MarketEnum
    tx = Transaction(
        portfolio_id=UUID(portfolio_id),
        symbol=symbol.upper(),
        market=MarketEnum[market.upper()],
        tx_type=TransactionType[tx_type.lower()],
        quantity=quantity,
        price=price,
        fx_rate=fx_rate,
        tx_date=tx_date,
        notes=notes,
    )
    db.add(tx)
    await db.flush()

    await _validate_no_short_position(
        portfolio_id, tx.symbol, tx.market.value, db,
    )

    from services.portfolio_cash_service import replace_transaction_settlement
    await replace_transaction_settlement(
        transaction=tx, db=db, reason="created",
    )

    # Rebuild holding from all transactions
    await _rebuild_holding(portfolio_id, symbol.upper(), market.upper(), db)
    return tx


async def import_transactions(
    *,
    portfolio_id: str,
    user_id: str,
    rows: list[dict[str, Any]],
    dry_run: bool,
    db: AsyncSession,
) -> dict[str, Any]:
    """Validate a broker-neutral batch before writing any transaction.

    Structural failures and chronological oversells are returned with the
    one-based CSV row number (header is row 1). A commit is attempted only
    when the complete batch is clean, so the request transaction stays
    all-or-nothing.
    """
    from pydantic import ValidationError

    from api.portfolio.schemas import TransactionCreate

    portfolio = await _lock_owned_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")

    parsed: list[tuple[int, TransactionCreate]] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=2):
        try:
            parsed.append((index, TransactionCreate.model_validate(raw)))
        except ValidationError as exc:
            for issue in exc.errors(include_url=False):
                location = issue.get("loc", ())
                errors.append({
                    "row": index,
                    "field": str(location[0]) if location else None,
                    "message": issue["msg"],
                })

    # Hash normalized Pydantic output rather than raw CSV spellings, so a
    # retry remains identical across aliases/casing and 1 vs 1.0. Preserve
    # row order because same-day buy/sell ordering is semantically relevant.
    content_hash: str | None = None
    if not errors:
        canonical_rows = []
        for _, item in parsed:
            normalized = item.model_dump(mode="json")
            normalized["symbol"] = item.symbol.upper()
            if normalized["fx_rate"] == 0:
                normalized["fx_rate"] = None
            canonical_rows.append(normalized)
        payload = json.dumps(
            canonical_rows, sort_keys=True, separators=(",", ":"),
        ).encode()
        content_hash = hashlib.sha256(payload).hexdigest()
        previous = await db.scalar(
            select(PortfolioTransactionImport).where(
                PortfolioTransactionImport.portfolio_id == UUID(portfolio_id),
                PortfolioTransactionImport.content_hash == content_hash,
            )
        )
        if previous:
            return {
                "valid": True,
                "valid_count": previous.row_count,
                "imported_count": 0,
                "duplicate": True,
                "import_id": str(previous.id),
                "imported_at": previous.created_at,
                "errors": [],
            }

    # Replay existing and candidate trades chronologically. Existing trades
    # precede imported rows on the same date; imported rows retain CSV order.
    existing = list((await db.scalars(
        select(Transaction)
        .where(Transaction.portfolio_id == UUID(portfolio_id))
        .order_by(Transaction.tx_date, Transaction.created_at, Transaction.id)
    )).all())
    ledger: dict[tuple[str, str], float] = {}
    timeline: list[tuple[date, int, int, str, str, str, float, int | None]] = []
    for order, tx in enumerate(existing):
        timeline.append((
            tx.tx_date, 0, order, tx.symbol, tx.market.value,
            tx.tx_type.value, float(tx.quantity), None,
        ))
    for order, (row_number, tx) in enumerate(parsed):
        timeline.append((
            tx.tx_date, 1, order, tx.symbol.upper(), tx.market.upper(),
            tx.tx_type.lower(), tx.quantity, row_number,
        ))
    rejected_rows: set[int] = set()
    for _, _, _, symbol, market, tx_type, quantity, row_number in sorted(
        timeline, key=lambda item: item[:3],
    ):
        key = (symbol, market)
        balance = ledger.get(key, 0.0)
        if tx_type == "buy":
            ledger[key] = balance + quantity
        elif tx_type == "sell":
            if balance - quantity < -1e-9:
                if row_number is not None and row_number not in rejected_rows:
                    errors.append({
                        "row": row_number,
                        "field": "quantity",
                        "message": "Sell quantity exceeds available shares at transaction date",
                    })
                    rejected_rows.add(row_number)
                continue
            ledger[key] = balance - quantity

    valid_count = len(parsed) - len(rejected_rows)
    if errors or dry_run:
        return {
            "valid": not errors,
            "valid_count": valid_count if not errors else max(0, valid_count),
            "imported_count": 0,
            "duplicate": False,
            "import_id": None,
            "imported_at": None,
            "errors": errors,
        }

    # Write the validated batch directly, then rebuild each affected holding
    # once. Calling add_transaction for every row would repeatedly re-query
    # ownership and replay the same growing history (quadratic at the 500-row
    # limit). FX lookups are shared by market/date within this import.
    from models.portfolio import Market as MarketEnum
    from services.portfolio_cash_service import replace_transaction_settlement

    fx_cache: dict[tuple[str, date], float] = {}
    transactions: list[Transaction] = []
    affected: set[tuple[str, str]] = set()
    assert content_hash is not None
    import_record = PortfolioTransactionImport(
        portfolio_id=UUID(portfolio_id),
        content_hash=content_hash,
        row_count=len(parsed),
    )
    db.add(import_record)
    await db.flush()
    for _, item in sorted(parsed, key=lambda entry: (entry[1].tx_date, entry[0])):
        market = item.market.upper()
        fx_rate = item.fx_rate
        if fx_rate is None or fx_rate == 0:
            cache_key = (market, item.tx_date)
            if cache_key not in fx_cache:
                fx_cache[cache_key] = await get_default_fx_rate(
                    market, portfolio.currency, item.tx_date,
                )
            fx_rate = fx_cache[cache_key]
        transaction = Transaction(
            portfolio_id=UUID(portfolio_id),
            import_id=import_record.id,
            symbol=item.symbol.upper(),
            market=MarketEnum[market],
            tx_type=TransactionType[item.tx_type.lower()],
            quantity=item.quantity,
            price=item.price,
            fx_rate=fx_rate,
            tx_date=item.tx_date,
            notes=item.notes,
        )
        db.add(transaction)
        transactions.append(transaction)
        affected.add((transaction.symbol, market))
    await db.flush()
    for transaction in transactions:
        await replace_transaction_settlement(
            transaction=transaction, db=db, reason="csv_import",
        )
    for symbol, market in affected:
        await _rebuild_holding(portfolio_id, symbol, market, db)
    return {
        "valid": True,
        "valid_count": len(parsed),
        "imported_count": len(parsed),
        "duplicate": False,
        "import_id": str(import_record.id),
        "imported_at": import_record.created_at,
        "errors": [],
    }


async def list_transaction_imports(
    *,
    portfolio_id: str,
    user_id: str,
    db: AsyncSession,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return newest import batches with linked-transaction completeness."""
    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")
    records = list((await db.scalars(
        select(PortfolioTransactionImport)
        .where(PortfolioTransactionImport.portfolio_id == portfolio.id)
        .order_by(PortfolioTransactionImport.created_at.desc())
        .limit(limit)
    )).all())
    import_ids = [record.id for record in records]
    aggregates: dict[UUID, tuple[int, date | None, date | None]] = {
        import_id: (count, first_tx_date, last_tx_date)
        for import_id, count, first_tx_date, last_tx_date in (await db.execute(
            select(
                Transaction.import_id,
                func.count(Transaction.id),
                func.min(Transaction.tx_date),
                func.max(Transaction.tx_date),
            )
            .where(Transaction.import_id.in_(import_ids))
            .group_by(Transaction.import_id)
        )).all()
    } if import_ids else {}
    instruments: dict[UUID, list[dict[str, str]]] = {}
    if import_ids:
        instrument_rows = (await db.execute(
            select(Transaction.import_id, Transaction.symbol, Transaction.market)
            .where(Transaction.import_id.in_(import_ids))
            .distinct()
            .order_by(Transaction.import_id, Transaction.market, Transaction.symbol)
        )).all()
        for import_id, symbol, market in instrument_rows:
            instruments.setdefault(import_id, []).append({
                "symbol": symbol,
                "market": market.value,
            })
    return [{
        "id": record.id,
        "row_count": record.row_count,
        "linked_count": aggregates.get(record.id, (0, None, None))[0],
        "provenance_complete": (
            record.row_count > 0
            and aggregates.get(record.id, (0, None, None))[0] == record.row_count
        ),
        "first_tx_date": aggregates.get(record.id, (0, None, None))[1],
        "last_tx_date": aggregates.get(record.id, (0, None, None))[2],
        "instruments": instruments.get(record.id, []),
        "imported_at": record.created_at,
    } for record in records]


async def get_transaction_import_transactions(
    *, portfolio_id: str, import_id: str, user_id: str, db: AsyncSession,
) -> list[Transaction]:
    """Return every transaction linked to one owned CSV import batch."""
    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")
    record = await db.scalar(select(PortfolioTransactionImport).where(
        PortfolioTransactionImport.id == UUID(import_id),
        PortfolioTransactionImport.portfolio_id == portfolio.id,
    ))
    if not record:
        raise ValueError("Transaction import not found")
    rows = await db.scalars(
        select(Transaction)
        .where(
            Transaction.portfolio_id == portfolio.id,
            Transaction.import_id == record.id,
        )
        .order_by(Transaction.tx_date, Transaction.created_at, Transaction.id)
    )
    return list(rows.all())


async def rollback_transaction_import(
    *,
    portfolio_id: str,
    import_id: str,
    user_id: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Reverse a complete imported batch without invalidating later history."""
    portfolio = await _lock_owned_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")
    record = await db.scalar(
        select(PortfolioTransactionImport)
        .where(
            PortfolioTransactionImport.id == UUID(import_id),
            PortfolioTransactionImport.portfolio_id == portfolio.id,
        )
        .with_for_update()
    )
    if not record:
        raise ValueError("Transaction import not found")
    transactions = list((await db.scalars(
        select(Transaction)
        .where(Transaction.import_id == record.id)
        .order_by(Transaction.tx_date, Transaction.created_at, Transaction.id)
        .with_for_update()
    )).all())
    if not transactions or len(transactions) != record.row_count:
        raise ValueError(
            "Import batch provenance is incomplete and cannot be rolled back"
        )

    affected = {(tx.symbol, tx.market.value) for tx in transactions}
    excluded_ids = {tx.id for tx in transactions}
    for symbol, market in affected:
        await _validate_no_short_position(
            portfolio_id, symbol, market, db,
            exclude_transaction_ids=excluded_ids,
        )

    from services.portfolio_cash_service import reverse_transaction_settlement

    for transaction in transactions:
        await reverse_transaction_settlement(
            transaction=transaction, db=db, reason="csv_import_rollback",
        )
        await db.delete(transaction)
    await db.flush()
    for symbol, market in affected:
        await _rebuild_holding(portfolio_id, symbol, market, db)
    await db.delete(record)
    await db.flush()
    return {"import_id": record.id, "removed_count": len(transactions)}


async def update_transaction(
    portfolio_id: str,
    tx_id: str,
    user_id: str,
    db: AsyncSession,
    *,
    symbol: str | None = None,
    market: str | None = None,
    tx_type: str | None = None,
    quantity: float | None = None,
    price: float | None = None,
    fx_rate: float | None = None,
    tx_date: date | None = None,
    notes: str | None = None,
) -> Transaction | None:
    """Patch fields on an existing transaction.

    If symbol or market changed, both the OLD and NEW (symbol, market)
    holdings are rebuilt — otherwise the old holding would still reflect
    the now-removed transaction.
    """
    p = await _lock_owned_portfolio(portfolio_id, user_id, db)
    if not p:
        return None
    tx = await db.get(Transaction, UUID(tx_id))
    if not tx or str(tx.portfolio_id) != portfolio_id:
        return None

    from models.paper_trading import PaperFill

    if await db.scalar(select(PaperFill.id).where(PaperFill.transaction_id == tx.id)):
        raise ValueError("Paper trading fill transactions are immutable")

    from models.portfolio import Market as MarketEnum
    old_symbol = tx.symbol
    old_market = tx.market.value
    settlement_changed = any(value is not None for value in (
        market, tx_type, quantity, price, fx_rate, tx_date,
    ))

    if symbol is not None:
        tx.symbol = symbol.upper()
    if market is not None:
        tx.market = MarketEnum[market.upper()]
    if tx_type is not None:
        tx.tx_type = TransactionType[tx_type.lower()]
    if quantity is not None:
        tx.quantity = quantity
    if price is not None:
        tx.price = price
    if fx_rate is not None:
        tx.fx_rate = fx_rate
    if tx_date is not None:
        tx.tx_date = tx_date
    if notes is not None:
        tx.notes = notes

    # If the caller changed tx_date or market without pinning a new
    # fx_rate, refresh it from the historical rate so the cost basis
    # stays consistent with the new trade-day.
    if (tx_date is not None or market is not None) and fx_rate is None:
        tx.fx_rate = await get_default_fx_rate(
            tx.market.value, p.currency, tx.tx_date,
        )

    await db.flush()

    await _validate_no_short_position(
        portfolio_id, tx.symbol, tx.market.value, db,
    )
    if old_symbol != tx.symbol or old_market != tx.market.value:
        await _validate_no_short_position(
            portfolio_id, old_symbol, old_market, db,
        )

    if settlement_changed:
        from services.portfolio_cash_service import replace_transaction_settlement
        await replace_transaction_settlement(
            transaction=tx, db=db, reason="updated",
        )

    await _rebuild_holding(portfolio_id, tx.symbol, tx.market.value, db)
    if old_symbol != tx.symbol or old_market != tx.market.value:
        await _rebuild_holding(portfolio_id, old_symbol, old_market, db)
    return tx


async def delete_transaction(
    portfolio_id: str, tx_id: str, user_id: str, db: AsyncSession,
) -> bool:
    """Remove a transaction. Rebuilds the affected holding from remaining txs."""
    p = await _lock_owned_portfolio(portfolio_id, user_id, db)
    if not p:
        return False
    tx = await db.get(Transaction, UUID(tx_id))
    if not tx or str(tx.portfolio_id) != portfolio_id:
        return False

    from models.paper_trading import PaperFill

    if await db.scalar(select(PaperFill.id).where(PaperFill.transaction_id == tx.id)):
        raise ValueError("Paper trading fill transactions are immutable")

    symbol = tx.symbol
    market = tx.market.value
    await _validate_no_short_position(
        portfolio_id, symbol, market, db, exclude_transaction_id=tx.id,
    )
    from services.portfolio_cash_service import reverse_transaction_settlement
    await reverse_transaction_settlement(
        transaction=tx, db=db, reason="transaction_deleted",
    )
    await db.delete(tx)
    await db.flush()
    await _rebuild_holding(portfolio_id, symbol, market, db)
    return True


async def _rebuild_holding(portfolio_id: str, symbol: str, market: str, db: AsyncSession) -> None:
    """Recalculate average cost and quantity from transaction history."""
    from models.portfolio import Market as MarketEnum
    txs = await db.scalars(
        select(Transaction)
        .where(
            Transaction.portfolio_id == UUID(portfolio_id),
            Transaction.symbol == symbol,
            Transaction.market == MarketEnum[market],
        )
        .order_by(Transaction.tx_date)
    )
    txs = list(txs)

    qty = 0.0
    cost_basis = 0.0
    for tx in txs:
        if tx.tx_type == TransactionType.buy:
            cost_basis = (cost_basis * qty + float(tx.price) * float(tx.quantity)) / (qty + float(tx.quantity))
            qty += float(tx.quantity)
        elif tx.tx_type == TransactionType.sell:
            qty = max(0.0, qty - float(tx.quantity))

    # Delete existing holding and recreate
    from models.portfolio import Market as MarketEnum
    existing = await db.scalar(
        select(Holding).where(
            Holding.portfolio_id == UUID(portfolio_id),
            Holding.symbol == symbol,
            Holding.market == MarketEnum[market],
        )
    )
    if existing:
        await db.delete(existing)

    if qty > 1e-9:
        cost_currency = "TWD" if market == "TW" else "USD"
        h = Holding(
            portfolio_id=UUID(portfolio_id),
            symbol=symbol,
            market=MarketEnum[market],
            quantity=qty,
            avg_cost=cost_basis,
            cost_currency=cost_currency,
        )
        db.add(h)


async def _validate_no_short_position(
    portfolio_id: str, symbol: str, market: str, db: AsyncSession,
    *,
    exclude_transaction_id: UUID | None = None,
    exclude_transaction_ids: set[UUID] | None = None,
) -> None:
    """Reject a transaction history that sells shares before they exist."""
    from models.portfolio import Market as MarketEnum

    stmt = select(Transaction).where(
        Transaction.portfolio_id == UUID(portfolio_id),
        Transaction.symbol == symbol,
        Transaction.market == MarketEnum[market],
    )
    if exclude_transaction_id is not None:
        stmt = stmt.where(Transaction.id != exclude_transaction_id)
    if exclude_transaction_ids:
        stmt = stmt.where(Transaction.id.not_in(exclude_transaction_ids))
    txs = list((await db.scalars(stmt.order_by(
        Transaction.tx_date, Transaction.created_at, Transaction.id,
    ))).all())
    quantity = 0.0
    for item in txs:
        if item.tx_type == TransactionType.buy:
            quantity += float(item.quantity)
        elif item.tx_type == TransactionType.sell:
            quantity -= float(item.quantity)
            if quantity < -1e-9:
                raise ValueError(
                    "Sell quantity exceeds available shares at transaction date"
                )


async def _cost_value_in_portfolio_currency(
    *,
    portfolio_id: str,
    symbol: str,
    market: str,
    portfolio_currency: str,
    cost_currency: str,
    db: AsyncSession,
) -> float | None:
    """
    Replay this holding's transactions in chronological order, valuing
    each buy at `price * qty * fx_rate` (the historical rate stored on
    that transaction). Returns the remaining-shares cost basis in the
    portfolio's currency.

    Sells reduce the cost basis at the running average — the same
    convention the in-memory rebuild uses. Returns None if the
    transaction history is missing or the holding has zero remaining
    shares (caller falls back to current-rate conversion).
    """
    from models.portfolio import Market as MarketEnum
    txs = await db.scalars(
        select(Transaction)
        .where(
            Transaction.portfolio_id == UUID(portfolio_id),
            Transaction.symbol == symbol,
            Transaction.market == MarketEnum[market],
        )
        .order_by(Transaction.tx_date, Transaction.created_at)
    )
    txs = list(txs)
    if not txs:
        return None

    qty = 0.0
    avg_pc = 0.0   # average cost per share, in portfolio currency
    same_currency = (
        _normalize_currency(cost_currency) == _normalize_currency(portfolio_currency)
    )
    for tx in txs:
        tx_qty = float(tx.quantity)
        tx_price = float(tx.price)
        if same_currency:
            # If the trade currency matches the portfolio currency, skip the
            # FX multiplier even when fx_rate happens to be != 1 (legacy
            # data from before auto-stamping).
            rate = 1.0
        else:
            stored = float(tx.fx_rate or 0.0)
            # Across a real currency pair (TWD↔USD), fx_rate=1.0 is
            # implausible — almost certainly a frontend default that was
            # submitted before the auto-suggest fetch landed, or a legacy
            # row from before fx_rate auto-stamping. Trusting it would
            # treat the foreign-currency cost basis as if it were already
            # in the portfolio currency (e.g. 125,000 TWD == 125,000 USD).
            # Re-derive at compute time from the historical rate.
            if stored > 0 and stored != 1.0:
                rate = stored
            else:
                rate = await get_default_fx_rate(
                    market=str(tx.market.value),
                    portfolio_currency=portfolio_currency,
                    tx_date=tx.tx_date,
                )
        if tx.tx_type == TransactionType.buy:
            new_qty = qty + tx_qty
            if new_qty <= 0:
                continue
            avg_pc = (avg_pc * qty + tx_price * tx_qty * rate) / new_qty
            qty = new_qty
        elif tx.tx_type == TransactionType.sell:
            qty = max(0.0, qty - tx_qty)
            if qty == 0:
                avg_pc = 0.0

    if qty <= 1e-9:
        return None
    return qty * avg_pc


# ── P&L calculation ───────────────────────────────────────────────

async def get_portfolio_detail(portfolio_id: str, user_id: str, db: AsyncSession) -> dict[str, Any]:
    p = await get_portfolio(portfolio_id, user_id, db)
    if not p:
        raise ValueError("Portfolio not found")

    holdings = await db.scalars(select(Holding).where(Holding.portfolio_id == UUID(portfolio_id)))
    holdings = list(holdings)

    # Fetch current prices concurrently
    async def _enrich(h: Holding) -> dict:
        try:
            return await _enrich_one(h, p)
        except Exception:
            # A single holding's failure (quote upstream blew up, FX
            # lookup raised, transaction replay hit a bad row, …) used
            # to propagate through asyncio.gather and 500 the entire
            # portfolio detail load. Log the symbol + traceback and
            # return a degraded row so the rest of the portfolio still
            # renders. Caller treats `current_price = avg_cost` as
            # "no live quote available".
            logger.exception(
                "portfolio.enrich_holding_failed",
                extra={
                    "portfolio_id": portfolio_id,
                    "symbol": h.symbol,
                    "market": str(h.market.value),
                },
            )
            qty = float(h.quantity)
            avg = float(h.avg_cost)
            try:
                cost_pc = await _to_portfolio_currency(qty * avg, h.cost_currency, p.currency)
            except Exception:
                cost_pc = qty * avg
            return {
                "id": str(h.id),
                "symbol": h.symbol,
                "market": str(h.market.value),
                "quantity": qty,
                "avg_cost": avg,
                "cost_currency": h.cost_currency,
                "current_price": avg,   # degraded — no live quote
                "current_value": round(cost_pc, 2),
                "cost_value": round(cost_pc, 2),
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
            }

    async def _enrich_one(h: Holding, p: Portfolio) -> dict:
        try:
            mkt = str(h.market.value)
            if mkt == "US":
                q = await us_quote(h.symbol)
            elif mkt == "CRYPTO":
                q = await crypto_quote(h.symbol)
            else:
                q = await tw_quote(h.symbol)
            current_price = q.get("price", 0)
        except Exception:
            current_price = float(h.avg_cost)

        qty = float(h.quantity)
        avg = float(h.avg_cost)
        curr_val = qty * current_price
        curr_val_pc = await _to_portfolio_currency(curr_val, h.cost_currency, p.currency)

        # Cost basis uses each transaction's stored fx_rate (the rate on
        # trade day) so cross-currency portfolios show what the holding
        # actually cost at the time, not what it would cost if rebought
        # today. Falls back to the cost-currency average × current FX
        # when transactions for some reason have no usable fx_rate.
        cost_val_pc = await _cost_value_in_portfolio_currency(
            portfolio_id=str(h.portfolio_id),
            symbol=h.symbol,
            market=str(h.market.value),
            portfolio_currency=p.currency,
            cost_currency=h.cost_currency,
            db=db,
        )
        if cost_val_pc is None:
            cost_val_pc = await _to_portfolio_currency(qty * avg, h.cost_currency, p.currency)

        unrealized_pnl = curr_val_pc - cost_val_pc
        unrealized_pnl_pct = unrealized_pnl / cost_val_pc * 100 if cost_val_pc else 0.0

        return {
            "id": str(h.id),
            "symbol": h.symbol,
            "market": str(h.market.value),
            "quantity": qty,
            "avg_cost": avg,
            "cost_currency": h.cost_currency,
            "current_price": current_price,
            "current_value": round(curr_val_pc, 2),
            "cost_value": round(cost_val_pc, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 4),
        }

    enriched = await asyncio.gather(*[_enrich(h) for h in holdings])

    total_value = sum(h["current_value"] for h in enriched)
    total_cost  = sum(h["cost_value"]    for h in enriched)
    total_pnl   = total_value - total_cost
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0.0

    from services.portfolio_cash_service import (
        cash_value_in_currency,
        get_cash_balances,
    )
    cash_balances = await get_cash_balances(
        portfolio_id=portfolio_id, user_id=user_id, db=db,
    )
    cash_value = await cash_value_in_currency(
        balances=cash_balances, target_currency=p.currency,
    )
    net_liquidation_value = total_value + cash_value

    # Preserve the legacy holdings-only weight and add the economically
    # complete weight against holdings + ledger cash.
    for h in enriched:
        h["weight_pct"] = round(h["current_value"] / total_value * 100, 2) if total_value else 0.0
        h["net_weight_pct"] = (
            round(h["current_value"] / net_liquidation_value * 100, 2)
            if net_liquidation_value > 0 else 0.0
        )

    return {
        "id": portfolio_id,
        "name": p.name,
        "currency": p.currency,
        "total_value": round(total_value, 2),
        "total_cost":  round(total_cost, 2),
        "total_pnl":   round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "cash_balances": cash_balances,
        "cash_value": round(cash_value, 2),
        "net_liquidation_value": round(net_liquidation_value, 2),
        "holdings": sorted(enriched, key=lambda x: x["current_value"], reverse=True),
    }


# ── Optimisation ──────────────────────────────────────────────────


async def get_transactions(
    portfolio_id: str, user_id: str, db: AsyncSession, limit: int = 200
) -> list[Transaction]:
    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")
    rows = await db.scalars(
        select(Transaction)
        .where(Transaction.portfolio_id == portfolio.id)
        .order_by(Transaction.tx_date.desc(), Transaction.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())


async def get_portfolio_snapshots(
    portfolio_id: str, user_id: str, db: AsyncSession, *, days: int = 90,
) -> list[PortfolioSnapshot]:
    portfolio = await get_portfolio(portfolio_id, user_id, db)
    if not portfolio:
        raise ValueError("Portfolio not found")
    cutoff = date.today() - timedelta(days=days - 1)
    rows = await db.scalars(select(PortfolioSnapshot).where(
        PortfolioSnapshot.portfolio_id == UUID(portfolio_id),
        PortfolioSnapshot.snapshot_date >= cutoff,
    ).order_by(PortfolioSnapshot.snapshot_date.desc()))
    return list(rows.all())


# Portfolio analytics extracted to portfolio_analytics.py.
# Re-export for back-compat with `api/portfolio/router.py`.
from services.portfolio_analytics import (  # noqa: E402,F401
    get_performance,
    optimise_portfolio,
)
