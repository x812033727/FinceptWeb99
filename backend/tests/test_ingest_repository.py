"""Round-trip tests for the OHLCV repository.

Exercises insert / upsert / range-read against the in-memory SQLite test
engine. Health-snapshot tests live in test_admin_ingest_health.py because
they need the API client.
"""
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from services.ingest.repository import (
    OhlcvBar,
    read_ohlcv_range,
    upsert_ohlcv_bars,
)


def _bar(symbol: str, ts: date, close: float = 100.0, source: str = "twse") -> OhlcvBar:
    return OhlcvBar(
        market="TW", symbol=symbol, ts=ts,
        open=close - 1, high=close + 1, low=close - 2, close=close, volume=1_000,
        source=source,
    )


@pytest.mark.asyncio
async def test_upsert_inserts_new_rows(db_session: AsyncSession):
    bars = [_bar("2330", date(2026, 4, 1), 600.0), _bar("2330", date(2026, 4, 2), 602.0)]
    written = await upsert_ohlcv_bars(db_session, bars)
    assert written == 2

    rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "2330")
    )).all()
    assert len(rows) == 2
    assert {float(r.close) for r in rows} == {600.0, 602.0}


@pytest.mark.asyncio
async def test_upsert_overwrites_existing(db_session: AsyncSession):
    """Re-ingesting the same (market, symbol, ts) updates price/source."""
    await upsert_ohlcv_bars(db_session, [_bar("2317", date(2026, 4, 1), 100.0, "twse")])
    await upsert_ohlcv_bars(db_session, [_bar("2317", date(2026, 4, 1), 105.0, "finmind")])

    row = await db_session.scalar(
        select(OhlcvDaily).where(
            OhlcvDaily.symbol == "2317", OhlcvDaily.ts == date(2026, 4, 1)
        )
    )
    assert row is not None
    assert float(row.close) == 105.0
    assert row.source == "finmind"


@pytest.mark.asyncio
async def test_upsert_empty_iterable_is_noop(db_session: AsyncSession):
    written = await upsert_ohlcv_bars(db_session, [])
    assert written == 0


@pytest.mark.asyncio
async def test_read_ohlcv_range_returns_inclusive_window(db_session: AsyncSession):
    bars = [
        _bar("2330", date(2026, 3, 28), 590.0),
        _bar("2330", date(2026, 4, 1), 600.0),
        _bar("2330", date(2026, 4, 2), 602.0),
        _bar("2330", date(2026, 4, 5), 605.0),
    ]
    await upsert_ohlcv_bars(db_session, bars)

    out = await read_ohlcv_range(db_session, "TW", "2330", date(2026, 4, 1), date(2026, 4, 2))
    assert [b["time"] for b in out] == ["2026-04-01", "2026-04-02"]
    assert [b["close"] for b in out] == [600.0, 602.0]


@pytest.mark.asyncio
async def test_read_ohlcv_range_orders_ascending(db_session: AsyncSession):
    """Caller (charts, backtests) expects oldest-first."""
    bars = [
        _bar("0050", date(2026, 4, 3), 130.0),
        _bar("0050", date(2026, 4, 1), 128.0),
        _bar("0050", date(2026, 4, 2), 129.0),
    ]
    await upsert_ohlcv_bars(db_session, bars)

    out = await read_ohlcv_range(db_session, "TW", "0050", date(2026, 4, 1), date(2026, 4, 5))
    assert [b["time"] for b in out] == ["2026-04-01", "2026-04-02", "2026-04-03"]


@pytest.mark.asyncio
async def test_read_ohlcv_range_filters_by_market(db_session: AsyncSession):
    """Same symbol in a different market must not leak into TW results."""
    await upsert_ohlcv_bars(db_session, [_bar("2330", date(2026, 4, 1), 600.0)])
    us_bar = OhlcvBar(
        market="US", symbol="2330", ts=date(2026, 4, 1),
        open=10, high=11, low=9, close=10, volume=1, source="polygon",
    )
    await upsert_ohlcv_bars(db_session, [us_bar])

    tw = await read_ohlcv_range(db_session, "TW", "2330", date(2026, 4, 1), date(2026, 4, 1))
    assert len(tw) == 1
    assert tw[0]["close"] == 600.0


@pytest.mark.asyncio
async def test_read_ohlcv_range_returns_empty_when_no_bars(db_session: AsyncSession):
    out = await read_ohlcv_range(
        db_session, "TW", "DOES_NOT_EXIST", date(2026, 1, 1), date(2026, 12, 31),
    )
    assert out == []


def test_ohlcv_bar_from_connector_row_parses_iso_date():
    bar = OhlcvBar.from_connector_row(
        "TW", "2330", "twse",
        {"time": "2026-04-01", "open": 600, "high": 605, "low": 595, "close": 602, "volume": "1000"},
    )
    assert bar is not None
    assert bar.ts == date(2026, 4, 1)
    assert bar.volume == 1000
    assert bar.close == 602.0


def test_ohlcv_bar_from_connector_row_drops_malformed():
    """Connector rows missing time / with bad date are dropped, not raised."""
    assert OhlcvBar.from_connector_row("TW", "2330", "twse", {}) is None
    assert OhlcvBar.from_connector_row(
        "TW", "2330", "twse", {"time": "not-a-date"}
    ) is None
