"""
Pure-unit tests for historical FX lookup, auto-stamped fx_rate on
transactions, and cost-basis computation in portfolio currency from
per-transaction fx_rate.
"""
from datetime import date as _date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import services.portfolio_service as svc


# ── _get_historical_twd_usd ──────────────────────────────────────


@pytest.mark.asyncio
async def test_historical_twd_usd_returns_last_observation_in_window():
    """Walks back to find the latest observation on or before the date."""
    obs = [
        {"date": "2024-04-19", "value": 32.10},
        {"date": "2024-04-22", "value": 32.30},   # Monday after weekend
    ]
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch("data.us.fred_connector.get_series", new_callable=AsyncMock, return_value=obs):
        rate = await svc._get_historical_twd_usd(_date(2024, 4, 22))

    assert rate == pytest.approx(32.30)


@pytest.mark.asyncio
async def test_historical_twd_usd_returns_cached_value():
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value="31.55"), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch("data.us.fred_connector.get_series", new_callable=AsyncMock) as fred:
        rate = await svc._get_historical_twd_usd(_date(2024, 1, 5))

    assert rate == pytest.approx(31.55)
    fred.assert_not_called()


@pytest.mark.asyncio
async def test_historical_twd_usd_returns_none_on_empty_response():
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set", new_callable=AsyncMock), \
         patch("data.us.fred_connector.get_series", new_callable=AsyncMock, return_value=[]):
        rate = await svc._get_historical_twd_usd(_date(2024, 4, 22))

    assert rate is None


@pytest.mark.asyncio
async def test_historical_twd_usd_swallows_fred_error():
    with patch.object(svc, "cache_get", new_callable=AsyncMock, return_value=None), \
         patch("data.us.fred_connector.get_series", new_callable=AsyncMock,
               side_effect=RuntimeError("fred down")):
        rate = await svc._get_historical_twd_usd(_date(2024, 4, 22))

    assert rate is None


# ── get_default_fx_rate ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_fx_rate_same_currency_returns_one():
    rate = await svc.get_default_fx_rate("US", "USD", _date(2024, 4, 22))
    assert rate == 1.0


@pytest.mark.asyncio
async def test_default_fx_rate_us_in_twd_portfolio_uses_historical():
    """Bought AAPL in a TWD portfolio on 2024-04-22 → fx_rate ≈ 32.3."""
    with patch.object(svc, "_get_historical_twd_usd", new_callable=AsyncMock, return_value=32.3):
        rate = await svc.get_default_fx_rate("US", "TWD", _date(2024, 4, 22))
    assert rate == pytest.approx(32.3)


@pytest.mark.asyncio
async def test_default_fx_rate_tw_in_usd_portfolio_uses_inverse():
    """Bought 2330 in a USD portfolio → 1 / TWD-per-USD."""
    with patch.object(svc, "_get_historical_twd_usd", new_callable=AsyncMock, return_value=32.0):
        rate = await svc.get_default_fx_rate("TW", "USD", _date(2024, 4, 22))
    assert rate == pytest.approx(1 / 32.0)


@pytest.mark.asyncio
async def test_default_fx_rate_falls_back_to_current_when_historical_missing():
    with patch.object(svc, "_get_historical_twd_usd", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "_get_twd_usd_rate", new_callable=AsyncMock, return_value=31.5):
        rate = await svc.get_default_fx_rate("US", "TWD", _date(2024, 4, 22))
    assert rate == pytest.approx(31.5)


@pytest.mark.asyncio
async def test_default_fx_rate_returns_one_when_all_lookups_fail():
    with patch.object(svc, "_get_historical_twd_usd", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "_get_twd_usd_rate", new_callable=AsyncMock, return_value=0):
        rate = await svc.get_default_fx_rate("US", "TWD", _date(2024, 4, 22))
    assert rate == 1.0


@pytest.mark.asyncio
async def test_default_fx_rate_crypto_treats_usd():
    """Crypto trades USD-denominated → same-currency conversion in a USD
    portfolio → 1.0; cross-currency in a TWD portfolio uses historical."""
    same = await svc.get_default_fx_rate("CRYPTO", "USD", _date(2024, 4, 22))
    assert same == 1.0
    with patch.object(svc, "_get_historical_twd_usd", new_callable=AsyncMock, return_value=32.0):
        cross = await svc.get_default_fx_rate("CRYPTO", "TWD", _date(2024, 4, 22))
    assert cross == pytest.approx(32.0)


# ── _cost_value_in_portfolio_currency ─────────────────────────────


def _mock_tx(tx_type: str, qty: float, price: float, fx_rate: float, day: int):
    """Build a SQLAlchemy-row-like mock with the attributes the helper reads."""
    from models.portfolio import Market as MarketEnum, TransactionType
    tx = MagicMock()
    tx.tx_type = TransactionType[tx_type]
    tx.quantity = qty
    tx.price = price
    tx.fx_rate = fx_rate
    tx.tx_date = _date(2024, 4, day)
    tx.market = MarketEnum.US
    tx.symbol = "AAPL"
    return tx


def _mock_db_with_txs(txs: list) -> MagicMock:
    """db.scalars(...) returns an iterable of txs."""
    db = MagicMock()
    db.scalars = AsyncMock(return_value=iter(txs))
    return db


@pytest.mark.asyncio
async def test_cost_value_uses_historical_fx_per_buy():
    """Two buys at different rates → weighted-average cost in TWD."""
    txs = [
        _mock_tx("buy", qty=10, price=170.0, fx_rate=32.0, day=10),  # 10 × 170 × 32 = 54_400 TWD
        _mock_tx("buy", qty=10, price=180.0, fx_rate=33.0, day=20),  # 10 × 180 × 33 = 59_400 TWD
    ]
    db = _mock_db_with_txs(txs)

    cost = await svc._cost_value_in_portfolio_currency(
        portfolio_id=str(uuid4()), symbol="AAPL", market="US",
        portfolio_currency="TWD", cost_currency="USD", db=db,
    )

    # avg = (54_400 + 59_400) / 20 = 5_690; cost_val = 20 × 5_690 = 113_800
    assert cost == pytest.approx(113_800.0)


@pytest.mark.asyncio
async def test_cost_value_handles_partial_sell():
    """Sells reduce quantity; remaining cost = remaining_qty × avg_pc."""
    txs = [
        _mock_tx("buy",  qty=10, price=100.0, fx_rate=32.0, day=10),  # 10 × 100 × 32 = 32_000
        _mock_tx("sell", qty=4,  price=110.0, fx_rate=32.5, day=15),  # 6 left at avg 3_200
    ]
    db = _mock_db_with_txs(txs)
    cost = await svc._cost_value_in_portfolio_currency(
        portfolio_id=str(uuid4()), symbol="AAPL", market="US",
        portfolio_currency="TWD", cost_currency="USD", db=db,
    )
    # remaining_qty = 6, avg_pc = 32_000/10 = 3_200, cost = 19_200
    assert cost == pytest.approx(19_200.0)


@pytest.mark.asyncio
async def test_cost_value_returns_none_when_fully_sold():
    txs = [
        _mock_tx("buy",  qty=10, price=100.0, fx_rate=32.0, day=10),
        _mock_tx("sell", qty=10, price=110.0, fx_rate=32.5, day=15),
    ]
    db = _mock_db_with_txs(txs)
    cost = await svc._cost_value_in_portfolio_currency(
        portfolio_id=str(uuid4()), symbol="AAPL", market="US",
        portfolio_currency="TWD", cost_currency="USD", db=db,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_cost_value_skips_fx_when_currencies_match():
    """Same-currency holding with a stale non-1 fx_rate (legacy data)
    should still report cost based on price × qty only."""
    txs = [
        _mock_tx("buy", qty=10, price=100.0, fx_rate=32.0, day=10),
    ]
    db = _mock_db_with_txs(txs)
    cost = await svc._cost_value_in_portfolio_currency(
        portfolio_id=str(uuid4()), symbol="AAPL", market="US",
        portfolio_currency="USD", cost_currency="USD", db=db,
    )
    assert cost == pytest.approx(1_000.0)


@pytest.mark.asyncio
async def test_cost_value_returns_none_for_holding_with_no_history():
    db = _mock_db_with_txs([])
    cost = await svc._cost_value_in_portfolio_currency(
        portfolio_id=str(uuid4()), symbol="AAPL", market="US",
        portfolio_currency="TWD", cost_currency="USD", db=db,
    )
    assert cost is None


def _mock_tw_tx(tx_type: str, qty: float, price: float, fx_rate: float, day: int):
    """Same as _mock_tx but for a TW-market trade (TWD-priced)."""
    from models.portfolio import Market as MarketEnum, TransactionType
    tx = MagicMock()
    tx.tx_type = TransactionType[tx_type]
    tx.quantity = qty
    tx.price = price
    tx.fx_rate = fx_rate
    tx.tx_date = _date(2024, 4, day)
    tx.market = MarketEnum.TW
    tx.symbol = "00878"
    return tx


@pytest.mark.asyncio
async def test_cost_value_recovers_from_legacy_fx_rate_one_cross_currency():
    """Regression test for the calculation bug shown in the bug-report
    screenshot: a TW (TWD) buy stored with fx_rate=1.0 in a USD portfolio.

    Trusting the stored 1.0 would price the cost basis at 125,000 USD
    (the TWD figure). Re-deriving from the historical TWD/USD rate
    (≈32) recovers the real ~3,906 USD cost.
    """
    txs = [_mock_tw_tx("buy", qty=5_000, price=25.0, fx_rate=1.0, day=26)]
    db = _mock_db_with_txs(txs)

    with patch.object(
        svc, "get_default_fx_rate", new_callable=AsyncMock, return_value=1 / 32.0,
    ) as mock_default:
        cost = await svc._cost_value_in_portfolio_currency(
            portfolio_id=str(uuid4()), symbol="00878", market="TW",
            portfolio_currency="USD", cost_currency="TWD", db=db,
        )

    # 5_000 × 25 × (1/32) ≈ 3_906.25 USD
    assert cost == pytest.approx(3_906.25, rel=1e-4)
    mock_default.assert_awaited_once()


@pytest.mark.asyncio
async def test_cost_value_recovers_when_fx_rate_zero_or_missing():
    """fx_rate=0 (or treated as falsy) on a cross-currency tx must also
    trigger re-derivation rather than silently dropping the conversion."""
    txs = [_mock_tw_tx("buy", qty=1_000, price=100.0, fx_rate=0.0, day=10)]
    db = _mock_db_with_txs(txs)

    with patch.object(
        svc, "get_default_fx_rate", new_callable=AsyncMock, return_value=1 / 30.0,
    ):
        cost = await svc._cost_value_in_portfolio_currency(
            portfolio_id=str(uuid4()), symbol="00878", market="TW",
            portfolio_currency="USD", cost_currency="TWD", db=db,
        )

    # 1_000 × 100 / 30 ≈ 3_333.33 USD
    assert cost == pytest.approx(100_000 / 30.0)


@pytest.mark.asyncio
async def test_cost_value_keeps_realistic_stored_rate():
    """A realistic stored fx_rate (e.g. 32.0) is trusted as-is — no
    re-derivation. This preserves the historical-rate semantics for
    transactions that were correctly auto-stamped at trade time."""
    txs = [_mock_tx("buy", qty=10, price=170.0, fx_rate=32.0, day=10)]
    db = _mock_db_with_txs(txs)

    with patch.object(
        svc, "get_default_fx_rate", new_callable=AsyncMock,
    ) as mock_default:
        cost = await svc._cost_value_in_portfolio_currency(
            portfolio_id=str(uuid4()), symbol="AAPL", market="US",
            portfolio_currency="TWD", cost_currency="USD", db=db,
        )

    assert cost == pytest.approx(54_400.0)   # 10 × 170 × 32
    mock_default.assert_not_called()
