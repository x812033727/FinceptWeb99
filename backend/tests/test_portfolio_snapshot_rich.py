import json
import uuid
from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.portfolio import Holding, Market, Portfolio, PortfolioCashEntry, PortfolioSnapshot
from models.user import User, UserRole
from tasks import portfolio_snapshot as task


@pytest.mark.asyncio
async def test_daily_snapshot_persists_positions_cash_and_quality(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
):
    user = User(
        id=uuid.uuid4(), email="snapshot-rich@example.com",
        hashed_password="x", role=UserRole.viewer,
    )
    portfolio = Portfolio(
        id=uuid.uuid4(), user_id=user.id, name="Rich", currency="TWD",
    )
    portfolio_id = portfolio.id
    db_session.add_all([user, portfolio, Holding(
        portfolio_id=portfolio.id, symbol="2330", market=Market.TW,
        quantity=1_000, avg_cost=90, cost_currency="TWD",
    ), PortfolioCashEntry(
        portfolio_id=portfolio.id, currency="TWD", amount=20_000,
        entry_type="deposit", source="manual",
        occurred_on=date.today(),
    )])
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(task, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        task, "cache_get",
        AsyncMock(return_value=json.dumps({"price": 100})),
    )

    async def convert(amount: float, source: str, target: str) -> float:
        if source == "TWD" and target == "USD":
            return amount / 32
        return amount

    monkeypatch.setattr("services.portfolio_service._to_portfolio_currency", convert)
    await task._do_snapshots()

    db_session.expire_all()
    snapshot = await db_session.scalar(select(PortfolioSnapshot).where(
        PortfolioSnapshot.portfolio_id == portfolio_id,
    ))
    assert snapshot.base_currency == "TWD"
    assert snapshot.holdings_value_base == 100_000
    assert snapshot.cash_value_base == 20_000
    assert snapshot.total_value_base == 120_000
    assert snapshot.cash_balances == {"TWD": 20_000.0}
    assert snapshot.positions[0]["symbol"] == "2330"
    assert snapshot.valuation_quality == {
        "status": "complete", "missing_quote_symbols": [],
    }
    assert snapshot.total_value_usd == pytest.approx(3_750)
