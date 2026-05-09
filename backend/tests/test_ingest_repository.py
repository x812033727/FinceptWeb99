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
from models.tw_govt_bank_flow import TwGovtBankFlowDaily
from services.ingest.repository import (
    GovtBankFlowRow,
    OhlcvBar,
    read_ohlcv_range,
    upsert_govt_bank_flows,
    upsert_ohlcv_bars,
)
from sqlalchemy import func as sa_func


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


def test_ohlcv_bar_from_connector_row_drops_non_positive_close():
    """Listed equities never trade at 0; an upstream zero is junk."""
    assert OhlcvBar.from_connector_row(
        "TW", "2330", "twse",
        {"time": "2026-04-01", "open": 600, "high": 600, "low": 600, "close": 0},
    ) is None
    assert OhlcvBar.from_connector_row(
        "TW", "2330", "twse",
        {"time": "2026-04-01", "open": 600, "high": 600, "low": 600, "close": -1},
    ) is None


def test_ohlcv_bar_from_connector_row_drops_non_positive_open():
    assert OhlcvBar.from_connector_row(
        "TW", "2330", "twse",
        {"time": "2026-04-01", "open": 0, "high": 600, "low": 590, "close": 595},
    ) is None


def test_ohlcv_bar_from_connector_row_keeps_zero_volume():
    """Halt days have legitimate zero volume — must NOT be dropped."""
    bar = OhlcvBar.from_connector_row(
        "TW", "2330", "twse",
        {"time": "2026-04-01", "open": 600, "high": 600, "low": 600, "close": 600, "volume": 0},
    )
    assert bar is not None
    assert bar.volume == 0
    assert bar.close == 600.0


def test_ohlcv_bar_from_connector_row_allows_missing_close():
    """A row with close=None still passes (gets stored as NULL); only
    explicit non-positive numbers are rejected."""
    bar = OhlcvBar.from_connector_row(
        "TW", "2330", "twse",
        {"time": "2026-04-01", "open": 600, "high": 600, "low": 595, "close": None},
    )
    assert bar is not None
    assert bar.close is None


@pytest.mark.asyncio
async def test_chunked_upsert_handles_payloads_above_param_cap(
    db_session: AsyncSession,
):
    """Regression: govt-bank-flow ingest used to send ~13K rows × 6 cols
    in one INSERT, which exceeds asyncpg's 32767 bind-parameter ceiling
    and surfaces as `InterfaceError`. The chunked upsert helper must
    split the payload so the wire cap is never hit.

    SQLite has its own (looser) `SQLITE_LIMIT_VARIABLE_NUMBER` cap
    (default 32766 on >=3.32, 999 on older builds) so this test also
    catches a bug in the chunking logic itself when run against the
    in-memory test engine.
    """
    today = date(2026, 4, 1)
    rows = [
        GovtBankFlowRow(
            market="TW",
            ts=today,
            bank_name=f"bank_{i:05d}",
            buy_amount=i,
            sell_amount=i // 2,
            source="finmind",
        )
        for i in range(7000)
    ]
    written = await upsert_govt_bank_flows(db_session, rows)
    assert written == 7000

    count = await db_session.scalar(
        select(sa_func.count()).select_from(TwGovtBankFlowDaily),
    )
    assert count == 7000


# ── PR #215: bogus revenue growth filter ─────────────────────────


def test_is_bogus_growth_pair_detects_year_month_signature():
    """The PR #211 bug: connector mis-mapped FinMind's `revenue_year`
    (period year integer like 2026) to `revenue_yoy` and
    `revenue_month` (period month 1-12) to `revenue_mom`. Production
    rows from before the fix carry that exact signature. The detector
    must flag them precisely without false-positiving legitimate
    growth rates."""
    from services.ingest.repository import _is_bogus_growth_pair

    # Strict signature (PR #215): both yoy=year AND mom=month.
    assert _is_bogus_growth_pair(2026.0, 1.0, date(2026, 1, 1)) is True
    assert _is_bogus_growth_pair(2026.0, 2.0, date(2026, 2, 1)) is True
    assert _is_bogus_growth_pair(2024.0, 12.0, date(2024, 12, 1)) is True

    # Looser signature (PR #231): yoy alone matches year as exact int.
    # Production case: 1101 showed `+2026.0%` in 2026-XX with mom=None
    # (or some other value), bypassing the strict AND filter.
    assert _is_bogus_growth_pair(2026.0, None, date(2026, 4, 1)) is True
    assert _is_bogus_growth_pair(2026.0, 5.0, date(2026, 4, 1)) is True  # yoy=year, any mom
    assert _is_bogus_growth_pair(2024.0, None, date(2024, 7, 1)) is True

    # Legitimate growth rates — NOT flagged.
    assert _is_bogus_growth_pair(15.2, 3.5, date(2026, 4, 1)) is False
    assert _is_bogus_growth_pair(-8.0, 2.0, date(2026, 4, 1)) is False  # mom=month but yoy is normal
    assert _is_bogus_growth_pair(0.0, 0.0, date(2026, 4, 1)) is False
    # Non-integer yoy (typical `_pct_change` output) — even if numerically
    # close to the year, fractional component proves it was computed.
    assert _is_bogus_growth_pair(2026.5, None, date(2026, 4, 1)) is False
    assert _is_bogus_growth_pair(2025.9999, None, date(2026, 4, 1)) is False
    # yoy is integer but doesn't match THIS row's period year.
    assert _is_bogus_growth_pair(2025.0, None, date(2026, 4, 1)) is False
    assert _is_bogus_growth_pair(100.0, None, date(2026, 4, 1)) is False  # 100% growth, not year leak

    # Defensive: yoy=None always returns False (no signal at all).
    assert _is_bogus_growth_pair(None, 1.0, date(2026, 1, 1)) is False
    assert _is_bogus_growth_pair(None, None, date(2026, 1, 1)) is False


@pytest.mark.asyncio
async def test_top_revenue_growers_drops_bogus_year_month_rows(
    db_session: AsyncSession,
):
    """End-to-end: a 1101 row carrying the exact production-bug
    signature (yoy=2026, mom=1, ts=2026-02-01) gets filtered out at
    read time, so a sibling row with legitimate +15% YoY rises to
    the top instead of being buried under fabricated +2026%."""
    from services.ingest.repository import (
        RevenueMonthlyRow,
        read_top_revenue_growers,
        upsert_revenue_monthly,
    )

    ts = date(2026, 2, 1)
    await upsert_revenue_monthly(db_session, [
        # Bogus: matches PR #211 signature
        RevenueMonthlyRow(
            market="TW", symbol="1101", ts=ts,
            revenue=12_000_000_000,
            revenue_yoy=2026.0, revenue_mom=2.0,
            source="finmind",
        ),
        # Legitimate
        RevenueMonthlyRow(
            market="TW", symbol="2330", ts=ts,
            revenue=200_000_000_000,
            revenue_yoy=15.2, revenue_mom=3.0,
            source="finmind",
        ),
    ])

    top = await read_top_revenue_growers(db_session, market="TW", limit=10)
    syms = [r["symbol"] for r in top]
    # 1101 dropped (bogus signature), 2330 kept (real value).
    assert "1101" not in syms
    assert "2330" in syms
    assert top[0]["revenue_yoy"] == 15.2


@pytest.mark.asyncio
async def test_top_revenue_growers_drops_bogus_yoy_when_mom_is_null(
    db_session: AsyncSession,
):
    """Production regression: the discussion ctx surfaced 1101 with
    ``YoY +2026.0%`` even after PR #215 shipped. Cause: the strict
    AND-filter required ``mom == ts.month`` too, but in production
    the bogus row had ``mom=None`` (FinMind connector partial
    cleanup), so the filter missed it. PR #231 relaxes the filter to
    fire on ``yoy == ts.year`` alone when ``yoy`` is an exact integer.
    """
    from services.ingest.repository import (
        RevenueMonthlyRow,
        read_top_revenue_growers,
        upsert_revenue_monthly,
    )

    ts = date(2026, 2, 1)
    await upsert_revenue_monthly(db_session, [
        # Production-shaped bogus row: yoy=year leak, mom scrubbed to None.
        RevenueMonthlyRow(
            market="TW", symbol="1101", ts=ts,
            revenue=12_000_000_000,
            revenue_yoy=2026.0, revenue_mom=None,
            source="finmind",
        ),
        # Legitimate competitor — should bubble to the top.
        RevenueMonthlyRow(
            market="TW", symbol="2330", ts=ts,
            revenue=200_000_000_000,
            revenue_yoy=15.2, revenue_mom=3.0,
            source="finmind",
        ),
    ])

    top = await read_top_revenue_growers(db_session, market="TW", limit=10)
    syms = [r["symbol"] for r in top]
    assert "1101" not in syms, (
        "1101 with yoy=2026.0 mom=None must be filtered — otherwise "
        "personas surface fabricated +2026% growth in their analysis"
    )
    assert "2330" in syms
    assert top[0]["revenue_yoy"] == 15.2


@pytest.mark.asyncio
async def test_top_revenue_growers_masks_yoy_mom_in_backtest_mode(
    db_session: AsyncSession,
):
    """Backtest look-ahead protection (PR #256): the ingest task's
    `update_cols` includes `revenue_yoy` / `revenue_mom`, so a
    later backfill cron can RECOMPUTE these against revised
    baseline data. A backtest reading at as_of would then see the
    POST-revision number, not what was public on as_of.

    Mask the exact values in backtest mode while keeping the row
    in the response — the SET of top growers (which symbols rank
    high) is robust across minor restatements; the precise yoy
    number isn't. Personas can still cite "top revenue grower"
    without leaking a future-recomputed value.
    """
    from services.ingest.repository import (
        RevenueMonthlyRow,
        read_top_revenue_growers,
        upsert_revenue_monthly,
    )

    ts = date(2026, 2, 1)
    await upsert_revenue_monthly(db_session, [
        RevenueMonthlyRow(
            market="TW", symbol="2330", ts=ts,
            revenue=200_000_000_000,
            revenue_yoy=15.2, revenue_mom=3.0,
            source="finmind",
        ),
        RevenueMonthlyRow(
            market="TW", symbol="2454", ts=ts,
            revenue=50_000_000_000,
            revenue_yoy=8.4, revenue_mom=2.1,
            source="finmind",
        ),
    ])

    # Backtest mode: as_of is set → both yoy and mom must be None.
    top = await read_top_revenue_growers(
        db_session, market="TW", limit=10, as_of=ts,
    )
    assert len(top) >= 2
    # Ranking is preserved (2330 ahead of 2454 by yoy=15.2 vs 8.4)
    # — sorted internally before the mask is applied.
    assert top[0]["symbol"] == "2330"
    assert top[1]["symbol"] == "2454"
    # But the exact values are masked to avoid the leak.
    assert top[0]["revenue_yoy"] is None
    assert top[0]["revenue_mom"] is None
    assert top[1]["revenue_yoy"] is None
    assert top[1]["revenue_mom"] is None
    # Symbol + ts + revenue are preserved (those are insert-time data
    # that never get amended by the backfill cron).
    assert top[0]["revenue"] == 200_000_000_000


@pytest.mark.asyncio
async def test_top_revenue_growers_keeps_yoy_mom_in_live_mode(
    db_session: AsyncSession,
):
    """Live mode (as_of=None) must show real yoy/mom — the
    leak only matters for past-anchor reconstruction, and
    today's UI / chat consumers depend on the actual values."""
    from services.ingest.repository import (
        RevenueMonthlyRow,
        read_top_revenue_growers,
        upsert_revenue_monthly,
    )

    ts = date(2026, 2, 1)
    await upsert_revenue_monthly(db_session, [
        RevenueMonthlyRow(
            market="TW", symbol="2330", ts=ts,
            revenue=200_000_000_000,
            revenue_yoy=15.2, revenue_mom=3.0,
            source="finmind",
        ),
    ])

    top = await read_top_revenue_growers(db_session, market="TW", limit=10)
    assert len(top) >= 1
    assert top[0]["revenue_yoy"] == 15.2
    assert top[0]["revenue_mom"] == 3.0


@pytest.mark.asyncio
async def test_revenue_row_out_zeros_bogus_growth_in_range_read(
    db_session: AsyncSession,
):
    """The per-symbol revenue page (`read_revenue_range`) must also
    suppress the bogus pair so the user-facing table doesn't surface
    +2026%. We zero the values rather than dropping the row entirely
    — the revenue figure itself is still legitimate, only the growth
    pair was bogus."""
    from services.ingest.repository import (
        RevenueMonthlyRow,
        read_revenue_range,
        upsert_revenue_monthly,
    )

    ts = date(2026, 2, 1)
    await upsert_revenue_monthly(db_session, [
        RevenueMonthlyRow(
            market="TW", symbol="1101", ts=ts,
            revenue=12_252_892_000,
            revenue_yoy=2026.0, revenue_mom=2.0,
            source="finmind",
        ),
    ])

    rows = await read_revenue_range(
        db_session, "TW", "1101", date(2026, 1, 1), date(2026, 3, 1),
    )
    assert len(rows) == 1
    assert rows[0]["revenue"] == 12_252_892_000     # real revenue preserved
    assert rows[0]["revenue_yoy"] == 0.0            # bogus pair zeroed
    assert rows[0]["revenue_mom"] == 0.0
