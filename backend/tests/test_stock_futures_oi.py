"""Tests for the 個股期貨 ingest pipeline (PR #282).

Three layers:
  1. `_aggregate_to_per_stock_rows` — pure-compute, takes FinMind-
     shaped rows and produces `StockFuturesOiRow`s. Most of the
     logic worth pinning lives here (filter index futures,
     accumulate dealer sub-types, handle missing fields).
  2. `read_top_foreign_stock_futures_buyers` — DB read tier;
     5-trading-day delta + ranking.
  3. `upsert_stock_futures_oi` — bulk upsert idempotency.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_stock_futures_oi import TwStockFuturesOi
from services.ingest.repository import (
    StockFuturesOiRow,
    read_top_foreign_stock_futures_buyers,
    upsert_stock_futures_oi,
)
from tasks.ingest_stock_futures_oi_tw import (
    _INDEX_FUTURES_CONTRACTS,
    _aggregate_to_per_stock_rows,
)


def _fm_row(
    *,
    futures_id: str = "CDF",
    stock_id: str = "2330",
    investor: str = "外資",
    long_oi: int = 1000,
    short_oi: int = 200,
    ts: str = "2026-04-15",
) -> dict:
    """Synthesize a FinMind-shaped row matching the
    `TaiwanFuturesInstitutionalInvestors` schema."""
    return {
        "date":                              ts,
        "futures_id":                        futures_id,
        "stock_id":                          stock_id,
        "institutional_investors":           investor,
        "long_open_interest_balance_volume":  long_oi,
        "short_open_interest_balance_volume": short_oi,
    }


# ── _aggregate_to_per_stock_rows ─────────────────────────────────


def test_aggregate_emits_one_row_per_stock_per_day():
    """Three investor-type rows for the same (stock, date) collapse
    into ONE per-stock row with per-investor columns populated."""
    items = [
        _fm_row(investor="外資",    long_oi=2000, short_oi=300),
        _fm_row(investor="投信",    long_oi=500,  short_oi=100),
        _fm_row(investor="自營商",  long_oi=400,  short_oi=200),
    ]
    rows = _aggregate_to_per_stock_rows(items)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "2330"
    assert r.contract_id == "CDF"
    assert r.fini_long_oi == 2000
    assert r.fini_short_oi == 300
    assert r.fini_net_oi == 1700
    assert r.sitc_net_oi == 400
    assert r.dealer_net_oi == 200


def test_aggregate_filters_out_index_futures():
    """TX / MTX / TE / etc. are index futures — filter out so
    `tw_stock_futures_oi` only carries per-stock contracts.
    Pin every contract code in the hard-coded set."""
    items = []
    for contract in _INDEX_FUTURES_CONTRACTS:
        items.append(_fm_row(
            futures_id=contract, stock_id="",
        ))
    # Plus one legitimate stock-future row.
    items.append(_fm_row(futures_id="CDF", stock_id="2330"))

    rows = _aggregate_to_per_stock_rows(items)
    assert {r.contract_id for r in rows} == {"CDF"}


def test_aggregate_falls_back_to_contract_id_when_stock_id_missing():
    """FinMind sometimes returns rows with empty stock_id (older
    contracts / corrections). The aggregator falls back to using
    the contract code as the symbol so we don't drop the row."""
    items = [
        _fm_row(futures_id="CDF", stock_id="", investor="外資",
                long_oi=100, short_oi=50),
    ]
    rows = _aggregate_to_per_stock_rows(items)
    assert len(rows) == 1
    assert rows[0].symbol == "CDF"
    assert rows[0].contract_id == "CDF"


def test_aggregate_combines_dealer_sub_types():
    """自營商 has two sub-types (自行買賣 + 避險) — FinMind returns
    them as separate rows. The aggregator must SUM them into one
    dealer column."""
    items = [
        _fm_row(investor="自營商(自行買賣)", long_oi=300, short_oi=100),
        _fm_row(investor="自營商(避險)",     long_oi=400, short_oi=200),
    ]
    rows = _aggregate_to_per_stock_rows(items)
    assert len(rows) == 1
    r = rows[0]
    assert r.dealer_long_oi == 700
    assert r.dealer_short_oi == 300
    assert r.dealer_net_oi == 400


def test_aggregate_skips_unknown_investor_labels():
    """Some FinMind responses include 'Total' aggregate rows or
    other unmapped labels. Skip those silently rather than crash."""
    items = [
        _fm_row(investor="Total",  long_oi=999, short_oi=999),
        _fm_row(investor="外資",   long_oi=100, short_oi=50),
    ]
    rows = _aggregate_to_per_stock_rows(items)
    assert len(rows) == 1
    # Only the 外資 row contributed.
    assert rows[0].fini_long_oi == 100
    assert rows[0].sitc_long_oi is None


def test_aggregate_handles_missing_oi_fields():
    """A row with no long/short OI fields — possible when FinMind
    returns trade-volume-only rows. Aggregator preserves None
    rather than coercing to 0 (which would falsely look like a
    flat position)."""
    items = [
        {
            "date": "2026-04-15",
            "futures_id": "CDF",
            "stock_id": "2330",
            "institutional_investors": "外資",
            # No OI fields at all.
        },
    ]
    rows = _aggregate_to_per_stock_rows(items)
    assert len(rows) == 1
    assert rows[0].fini_long_oi is None
    assert rows[0].fini_net_oi is None


# ── read_top_foreign_stock_futures_buyers ────────────────────────


@pytest.mark.asyncio
async def test_read_top_buyers_ranks_by_5_day_fini_change(
    db_session: AsyncSession,
):
    """For each symbol, compute (latest_fini_net - earliest_fini_net)
    in the window and rank descending. 2330 grows +500, 2454 grows
    +200, 2317 grows -100 → expected order [2330, 2454, 2317]."""
    base = date(2026, 4, 15)
    later = date(2026, 4, 22)

    db_session.add_all([
        TwStockFuturesOi(
            market="TW", symbol="2330", contract_id="CDF", ts=base,
            fini_long_oi=1000, fini_short_oi=200, fini_net_oi=800,
            source="finmind",
        ),
        TwStockFuturesOi(
            market="TW", symbol="2330", contract_id="CDF", ts=later,
            fini_long_oi=1500, fini_short_oi=200, fini_net_oi=1300,
            source="finmind",
        ),
        TwStockFuturesOi(
            market="TW", symbol="2454", contract_id="GLF", ts=base,
            fini_long_oi=300, fini_short_oi=100, fini_net_oi=200,
            source="finmind",
        ),
        TwStockFuturesOi(
            market="TW", symbol="2454", contract_id="GLF", ts=later,
            fini_long_oi=500, fini_short_oi=100, fini_net_oi=400,
            source="finmind",
        ),
        TwStockFuturesOi(
            market="TW", symbol="2317", contract_id="CFF", ts=base,
            fini_long_oi=600, fini_short_oi=100, fini_net_oi=500,
            source="finmind",
        ),
        TwStockFuturesOi(
            market="TW", symbol="2317", contract_id="CFF", ts=later,
            fini_long_oi=550, fini_short_oi=150, fini_net_oi=400,
            source="finmind",
        ),
    ])
    await db_session.commit()

    out = await read_top_foreign_stock_futures_buyers(
        db_session, market="TW", days=10, limit=5,
        as_of=date(2026, 4, 22),
    )
    syms = [r["symbol"] for r in out]
    assert syms == ["2330", "2454", "2317"]
    # Top entry's delta is +500 vs start of window.
    assert out[0]["fini_change"] == 500
    assert out[0]["fini_net_oi"] == 1300


@pytest.mark.asyncio
async def test_read_top_buyers_skips_single_point_symbols(
    db_session: AsyncSession,
):
    """Symbol with only one data point in the window can't have a
    delta computed — must be silently dropped, not surfaced with
    `fini_change=0`."""
    db_session.add(TwStockFuturesOi(
        market="TW", symbol="2330", contract_id="CDF",
        ts=date(2026, 4, 15),
        fini_long_oi=1000, fini_short_oi=200, fini_net_oi=800,
        source="finmind",
    ))
    await db_session.commit()

    out = await read_top_foreign_stock_futures_buyers(
        db_session, market="TW", days=10, limit=5,
        as_of=date(2026, 4, 22),
    )
    assert out == []


@pytest.mark.asyncio
async def test_read_top_buyers_returns_empty_when_no_rows(
    db_session: AsyncSession,
):
    out = await read_top_foreign_stock_futures_buyers(
        db_session, market="TW", days=5, as_of=date(2026, 4, 22),
    )
    assert out == []


# ── upsert idempotency ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_overwrites_on_re_ingest(
    db_session: AsyncSession,
):
    """Re-running the cron on the same date corrects stale values
    in place. Pin the contract here — without ON CONFLICT DO
    UPDATE, the second insert would 23505 IntegrityError on PK."""
    base = date(2026, 4, 15)

    await upsert_stock_futures_oi(db_session, [
        StockFuturesOiRow(
            market="TW", symbol="2330", contract_id="CDF", ts=base,
            fini_long_oi=1000, fini_short_oi=200, fini_net_oi=800,
            sitc_long_oi=None, sitc_short_oi=None, sitc_net_oi=None,
            dealer_long_oi=None, dealer_short_oi=None, dealer_net_oi=None,
            source="finmind",
        ),
    ])
    # Same key, different values.
    n = await upsert_stock_futures_oi(db_session, [
        StockFuturesOiRow(
            market="TW", symbol="2330", contract_id="CDF", ts=base,
            fini_long_oi=2000, fini_short_oi=500, fini_net_oi=1500,
            sitc_long_oi=None, sitc_short_oi=None, sitc_net_oi=None,
            dealer_long_oi=None, dealer_short_oi=None, dealer_net_oi=None,
            source="finmind",
        ),
    ])
    assert n == 1
    # Verify the latest values won.
    from sqlalchemy import select as _select
    rows = (await db_session.scalars(
        _select(TwStockFuturesOi).where(
            TwStockFuturesOi.symbol == "2330",
        )
    )).all()
    assert len(rows) == 1
    assert rows[0].fini_net_oi == 1500
