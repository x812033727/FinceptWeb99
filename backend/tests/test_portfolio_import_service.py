"""Direct branch coverage for atomic portfolio transaction imports."""

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import services.portfolio_service as svc
from models.portfolio import (
    Holding,
    Portfolio,
    PortfolioCashEntry,
    PortfolioTransactionImport,
    Transaction,
)


async def _create_portfolio(
    client: AsyncClient, db: AsyncSession, email: str = "import-service@test.com",
) -> tuple[str, Portfolio]:
    await client.post(
        "/api/auth/register", json={"email": email, "password": "Test1234!"},
    )
    login = await client.post(
        "/api/auth/login", json={"email": email, "password": "Test1234!"},
    )
    token = login.json()["access_token"]
    response = await client.post(
        "/api/portfolio",
        json={"name": "Direct import", "currency": "USD"},
        headers={"Authorization": f"Bearer {token}"},
    )
    portfolio = await db.get(Portfolio, UUID(response.json()["id"]))
    assert portfolio is not None
    return token, portfolio


@pytest.mark.asyncio
async def test_import_service_writes_batch_and_shares_fx_lookup(
    client: AsyncClient, db_session: AsyncSession,
):
    _, portfolio = await _create_portfolio(client, db_session)
    rows = [
        {"tx_date": "2024-01-02", "symbol": "aapl", "market": "us",
         "tx_type": "buy", "quantity": 3, "price": 100},
        {"tx_date": "2024-01-02", "symbol": "AAPL", "market": "US",
         "tx_type": "buy", "quantity": 2, "price": 110},
    ]

    with patch(
        "services.portfolio_service.get_default_fx_rate",
        new_callable=AsyncMock,
        return_value=1.0,
    ) as get_fx:
        result = await svc.import_transactions(
            portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
            rows=rows, dry_run=False, db=db_session,
        )

    assert result["valid"] is True
    assert result["valid_count"] == 2
    assert result["imported_count"] == 2
    assert result["duplicate"] is False
    assert UUID(result["import_id"])
    assert result["imported_at"] is not None
    assert result["errors"] == []
    get_fx.assert_awaited_once_with("US", "USD", date(2024, 1, 2))
    transactions = list((await db_session.scalars(select(Transaction))).all())
    assert len(transactions) == 2
    assert {str(transaction.import_id) for transaction in transactions} == {
        result["import_id"]
    }
    assert {float(transaction.fx_rate) for transaction in transactions} == {1.0}
    holding = await db_session.scalar(select(Holding))
    assert holding is not None
    assert float(holding.quantity) == 5
    settlements = list((await db_session.scalars(select(PortfolioCashEntry))).all())
    assert len(settlements) == 2
    assert {entry.entry_type for entry in settlements} == {"trade_settlement"}

    # Equivalent normalized content (case, integer spelling, and 0 vs missing
    # auto-FX) returns the original import instead of duplicating accounting.
    duplicate = await svc.import_transactions(
        portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
        rows=[
            {"tx_date": "2024-01-02", "symbol": "AAPL", "market": "US",
             "tx_type": "BUY", "quantity": 3.0, "price": 100.0, "fx_rate": 0},
            {"tx_date": "2024-01-02", "symbol": "aapl", "market": "us",
             "tx_type": "buy", "quantity": 2.0, "price": 110.0},
        ],
        dry_run=False, db=db_session,
    )
    assert duplicate["duplicate"] is True
    assert duplicate["imported_count"] == 0
    assert duplicate["import_id"] == result["import_id"]
    assert len(list((await db_session.scalars(select(Transaction))).all())) == 2
    assert len(list((await db_session.scalars(select(PortfolioCashEntry))).all())) == 2
    records = list((await db_session.scalars(select(PortfolioTransactionImport))).all())
    assert len(records) == 1


@pytest.mark.asyncio
async def test_import_service_replays_existing_and_reports_every_bad_row(
    client: AsyncClient, db_session: AsyncSession,
):
    _, portfolio = await _create_portfolio(
        client, db_session, "import-service-errors@test.com",
    )
    await svc.import_transactions(
        portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
        rows=[{
            "tx_date": "2024-01-02", "symbol": "AAPL", "market": "US",
            "tx_type": "buy", "quantity": 5, "price": 100, "fx_rate": 1,
        }],
        dry_run=False, db=db_session,
    )

    result = await svc.import_transactions(
        portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
        rows=[
            {"tx_date": "2024-01-03", "symbol": "AAPL", "market": "US",
             "tx_type": "sell", "quantity": 4, "price": 110, "fx_rate": 1},
            {"tx_date": "2024-01-04", "symbol": "AAPL", "market": "US",
             "tx_type": "sell", "quantity": 2, "price": 111, "fx_rate": 1},
            {"tx_date": "bad-date", "symbol": "BAD!", "market": "EU",
             "tx_type": "buy", "quantity": 0, "price": -1},
        ],
        # Even an attempted commit remains read-only when any row is invalid.
        dry_run=False, db=db_session,
    )

    assert result["valid"] is False
    assert result["valid_count"] == 1
    assert {error["row"] for error in result["errors"]} == {3, 4}
    assert any(
        error["row"] == 3 and error["field"] == "quantity"
        for error in result["errors"]
    )
    transactions = list((await db_session.scalars(select(Transaction))).all())
    assert len(transactions) == 1


@pytest.mark.asyncio
async def test_import_service_valid_dry_run_is_read_only(
    client: AsyncClient, db_session: AsyncSession,
):
    _, portfolio = await _create_portfolio(
        client, db_session, "import-service-preview@test.com",
    )
    result = await svc.import_transactions(
        portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
        rows=[{
            "tx_date": "2024-01-02", "symbol": "MSFT", "market": "US",
            "tx_type": "buy", "quantity": 1, "price": 400,
        }],
        dry_run=True, db=db_session,
    )
    assert result == {
        "valid": True, "valid_count": 1, "imported_count": 0,
        "duplicate": False, "import_id": None, "imported_at": None, "errors": [],
    }
    assert await db_session.scalar(select(Transaction.id)) is None


@pytest.mark.asyncio
async def test_import_rollback_rejects_legacy_record_without_provenance(
    client: AsyncClient, db_session: AsyncSession,
):
    _, portfolio = await _create_portfolio(
        client, db_session, "import-service-legacy@test.com",
    )
    record = PortfolioTransactionImport(
        portfolio_id=portfolio.id, content_hash="a" * 64, row_count=1,
    )
    db_session.add(record)
    await db_session.flush()

    batches = await svc.list_transaction_imports(
        portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
        db=db_session,
    )
    assert batches[0]["linked_count"] == 0
    assert batches[0]["provenance_complete"] is False
    assert batches[0]["first_tx_date"] is None
    assert batches[0]["last_tx_date"] is None
    assert batches[0]["instruments"] == []
    with pytest.raises(ValueError, match="provenance is incomplete"):
        await svc.rollback_transaction_import(
            portfolio_id=str(portfolio.id), import_id=str(record.id),
            user_id=str(portfolio.user_id), db=db_session,
        )
    with pytest.raises(ValueError, match="Transaction import not found"):
        await svc.rollback_transaction_import(
            portfolio_id=str(portfolio.id), import_id=str(uuid4()),
            user_id=str(portfolio.user_id), db=db_session,
        )


@pytest.mark.asyncio
async def test_import_service_rolls_back_complete_multi_symbol_batch(
    client: AsyncClient, db_session: AsyncSession,
):
    _, portfolio = await _create_portfolio(
        client, db_session, "import-service-rollback@test.com",
    )
    imported = await svc.import_transactions(
        portfolio_id=str(portfolio.id), user_id=str(portfolio.user_id),
        rows=[
            {"tx_date": "2024-01-02", "symbol": "AAPL", "market": "US",
             "tx_type": "buy", "quantity": 2, "price": 100, "fx_rate": 1},
            {"tx_date": "2024-01-03", "symbol": "MSFT", "market": "US",
             "tx_type": "buy", "quantity": 3, "price": 200, "fx_rate": 1},
        ],
        dry_run=False, db=db_session,
    )

    result = await svc.rollback_transaction_import(
        portfolio_id=str(portfolio.id), import_id=imported["import_id"],
        user_id=str(portfolio.user_id), db=db_session,
    )
    assert result == {
        "import_id": UUID(imported["import_id"]), "removed_count": 2,
    }
    assert list((await db_session.scalars(select(Transaction))).all()) == []
    assert list((await db_session.scalars(select(Holding))).all()) == []
    entries = list((await db_session.scalars(select(PortfolioCashEntry))).all())
    assert len(entries) == 4
    assert sum(float(entry.amount) for entry in entries) == 0


@pytest.mark.asyncio
async def test_import_service_rejects_unknown_portfolio(db_session: AsyncSession):
    missing_portfolio = str(uuid4())
    missing_user = str(uuid4())
    with pytest.raises(ValueError, match="Portfolio not found"):
        await svc.import_transactions(
            portfolio_id=missing_portfolio, user_id=missing_user,
            rows=[{
                "tx_date": "2024-01-02", "symbol": "AAPL", "market": "US",
                "tx_type": "buy", "quantity": 1, "price": 100,
            }],
            dry_run=True, db=db_session,
        )
    with pytest.raises(ValueError, match="Portfolio not found"):
        await svc.list_transaction_imports(
            portfolio_id=missing_portfolio, user_id=missing_user, db=db_session,
        )
    with pytest.raises(ValueError, match="Portfolio not found"):
        await svc.rollback_transaction_import(
            portfolio_id=missing_portfolio, import_id=str(uuid4()),
            user_id=missing_user, db=db_session,
        )
