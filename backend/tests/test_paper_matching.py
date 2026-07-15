from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.paper_trading import PaperOrder
from models.portfolio import Portfolio
from services import paper_matching_service as matching
from services import paper_trading_service as trading


def _order(*, side: str = "buy", order_type: str = "limit", limit: float = 100) -> PaperOrder:
    return PaperOrder(
        portfolio_id=UUID("00000000-0000-0000-0000-000000000001"),
        symbol="AAPL",
        market="US",
        side=side,
        order_type=order_type,
        time_in_force="gtc",
        quantity=1,
        limit_price=limit if order_type == "limit" else None,
        reservation_price=limit,
        fee_bps=0,
        idempotency_key="matching-unit-order",
    )


def test_market_sessions_are_timezone_and_dst_aware():
    assert matching.is_market_open("US", datetime(2026, 7, 15, 14, 0, tzinfo=UTC))
    assert not matching.is_market_open("US", datetime(2026, 7, 18, 14, 0, tzinfo=UTC))
    assert matching.is_market_open("TW", datetime(2026, 7, 15, 2, 0, tzinfo=UTC))
    assert not matching.is_market_open("TW", datetime(2026, 7, 15, 6, 0, tzinfo=UTC))
    assert matching.is_market_open("CRYPTO", datetime(2026, 7, 18, 6, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        matching.is_market_open("US", datetime(2026, 7, 15, 14, 0))


@pytest.mark.parametrize(
    ("order", "quote", "expected"),
    [
        (_order(), {"ask": 99, "price": 101}, 99),
        (_order(), {"ask": 101}, None),
        (_order(side="sell"), {"bid": 101}, 101),
        (_order(side="sell"), {"bid": 99}, None),
        (_order(order_type="market"), {"price": 102}, 102),
        (_order(order_type="market"), {"price": float("nan")}, None),
        (_order(order_type="market"), {}, None),
    ],
)
def test_executable_price_uses_side_aware_quotes_and_limits(order, quote, expected):
    assert matching.executable_price(order, quote) == expected


def test_execution_plan_caps_volume_applies_slippage_and_preserves_limit():
    order = _order()
    order.quantity = 10
    plan = matching.plan_execution(
        order, {"ask": 99, "volume": 200, "ts": 123, "data_source": "test"}
    )
    assert plan is not None
    assert plan.quantity == 2
    assert plan.liquidity_quantity == 2
    assert plan.quote_price == 99
    assert plan.price == pytest.approx(99.099)
    assert plan.slippage_bps == pytest.approx(10)

    protected = matching.plan_execution(order, {"ask": 99.99, "ask_size": 5, "ts": 124})
    assert protected is not None
    assert protected.quantity == 5
    assert protected.price == 100
    assert protected.slippage_bps < 2


def test_quote_identity_is_stable_for_one_snapshot_and_changes_with_timestamp():
    quote = {"price": 100, "volume": 500, "ts": 123, "data_source": "test"}
    first = matching.quote_identity("US", "AAPL", quote)
    assert first == matching.quote_identity("US", "AAPL", dict(reversed(quote.items())))
    assert first != matching.quote_identity("US", "AAPL", {**quote, "ts": 124})


async def _login_and_portfolio(client, email: str) -> tuple[dict[str, str], str]:
    await client.post("/api/auth/register", json={"email": email, "password": "Test1234!"})
    login = await client.post("/api/auth/login", json={"email": email, "password": "Test1234!"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.post(
        "/api/portfolio", json={"name": "Matcher", "currency": "USD"}, headers=headers
    )
    portfolio_id = response.json()["id"]
    await client.post(
        f"/api/portfolio/{portfolio_id}/cash-entries",
        json={
            "currency": "USD",
            "amount": 1_000,
            "entry_type": "deposit",
            "idempotency_key": "matching-cash",
        },
        headers=headers,
    )
    return headers, portfolio_id


@pytest.mark.asyncio
async def test_match_order_fills_triggered_quote_and_leaves_untriggered_open(
    client, db_session: AsyncSession
):
    _, portfolio_id = await _login_and_portfolio(client, "matcher@example.com")
    portfolio = await db_session.get(Portfolio, UUID(portfolio_id))
    user_id = str(portfolio.user_id)
    order = await trading.submit_order(
        portfolio_id=portfolio_id,
        user_id=user_id,
        symbol="AAPL",
        market="US",
        side="buy",
        order_type="limit",
        quantity=2,
        limit_price=100,
        reference_price=None,
        fee_bps=0,
        idempotency_key="matching-order-001",
        notes=None,
        time_in_force="gtc",
        db=db_session,
    )
    open_time = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    assert (
        await matching.match_order(
            portfolio_id=portfolio_id,
            order_id=str(order.id),
            user_id=user_id,
            quote={"ask": 101},
            now=open_time,
            db=db_session,
        )
        is None
    )
    fill = await matching.match_order(
        portfolio_id=portfolio_id,
        order_id=str(order.id),
        user_id=user_id,
        quote={"ask": 99},
        now=open_time + timedelta(seconds=1),
        db=db_session,
    )
    assert fill is not None
    assert float(fill.price) == pytest.approx(99.099)
    assert float(fill.quantity) == 2
    assert float(fill.quote_price) == 99
    assert float(fill.slippage_bps) == pytest.approx(10)
    assert fill.execution_source == "quote"
    assert order.status == "filled"


@pytest.mark.asyncio
async def test_same_quote_snapshot_only_consumes_liquidity_once(client, db_session: AsyncSession):
    _, portfolio_id = await _login_and_portfolio(client, "snapshot-idempotency@example.com")
    portfolio = await db_session.get(Portfolio, UUID(portfolio_id))
    order = await trading.submit_order(
        portfolio_id=portfolio_id,
        user_id=str(portfolio.user_id),
        symbol="AAPL",
        market="US",
        side="buy",
        order_type="limit",
        quantity=10,
        limit_price=100,
        reference_price=None,
        fee_bps=0,
        idempotency_key="snapshot-liquidity-order",
        notes=None,
        time_in_force="gtc",
        db=db_session,
    )
    kwargs = {
        "portfolio_id": portfolio_id,
        "order_id": str(order.id),
        "user_id": str(portfolio.user_id),
        "now": datetime(2026, 7, 15, 14, 0, tzinfo=UTC),
        "db": db_session,
    }
    first = await matching.match_order(**kwargs, quote={"ask": 99, "volume": 200, "ts": 123})
    duplicate = await matching.match_order(**kwargs, quote={"ask": 99, "volume": 200, "ts": 123})
    next_snapshot = await matching.match_order(
        **kwargs, quote={"ask": 99, "volume": 200, "ts": 124}
    )
    assert first is not None and float(first.quantity) == 2
    assert duplicate is None
    assert next_snapshot is not None and float(next_snapshot.quantity) == 2
    assert float(order.filled_quantity) == 4
    assert order.status == "partially_filled"


@pytest.mark.asyncio
async def test_match_order_expires_day_order_and_rejects_closed_market(
    client, db_session: AsyncSession
):
    _, portfolio_id = await _login_and_portfolio(client, "expiry@example.com")
    portfolio = await db_session.get(Portfolio, UUID(portfolio_id))
    user_id = str(portfolio.user_id)
    order = await trading.submit_order(
        portfolio_id=portfolio_id,
        user_id=user_id,
        symbol="AAPL",
        market="US",
        side="buy",
        order_type="limit",
        quantity=1,
        limit_price=100,
        reference_price=None,
        fee_bps=0,
        idempotency_key="expiry-order-001",
        notes=None,
        time_in_force="day",
        db=db_session,
    )
    order.expires_at = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
    assert (
        await matching.match_order(
            portfolio_id=portfolio_id,
            order_id=str(order.id),
            user_id=user_id,
            quote={"ask": 99},
            now=datetime(2026, 7, 15, 20, 1, tzinfo=UTC),
            db=db_session,
        )
        is None
    )
    assert order.status == "expired"

    gtc = await trading.submit_order(
        portfolio_id=portfolio_id,
        user_id=user_id,
        symbol="MSFT",
        market="US",
        side="buy",
        order_type="market",
        quantity=1,
        limit_price=None,
        reference_price=100,
        fee_bps=0,
        idempotency_key="closed-order-001",
        notes=None,
        time_in_force="gtc",
        db=db_session,
    )
    with pytest.raises(matching.MarketClosedError, match="market is closed"):
        await matching.match_order(
            portfolio_id=portfolio_id,
            order_id=str(gtc.id),
            user_id=user_id,
            quote={"price": 100},
            now=datetime(2026, 7, 18, 14, 0, tzinfo=UTC),
            db=db_session,
        )


@pytest.mark.asyncio
async def test_match_endpoint_uses_live_matcher(client, db_session: AsyncSession, monkeypatch):
    headers, portfolio_id = await _login_and_portfolio(client, "endpoint@example.com")
    created = await client.post(
        f"/api/portfolio/{portfolio_id}/paper-orders",
        json={
            "symbol": "BTC-USD",
            "market": "CRYPTO",
            "side": "buy",
            "order_type": "market",
            "quantity": 0.01,
            "reference_price": 100,
            "time_in_force": "gtc",
            "idempotency_key": "endpoint-match-order",
        },
        headers=headers,
    )
    order_id = created.json()["id"]

    async def quote_stub(market, symbol):
        assert (market, symbol) == ("CRYPTO", "BTC-USD")
        return {"price": 100}

    monkeypatch.setattr(matching, "get_market_quote", quote_stub)
    response = await client.post(
        f"/api/portfolio/{portfolio_id}/paper-orders/{order_id}/match", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["price"] == pytest.approx(100.1)
    assert response.json()["quote_price"] == 100
    assert response.json()["slippage_bps"] == pytest.approx(10)
    assert response.json()["execution_source"] == "quote"
