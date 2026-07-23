"""Backtest focus brief — the as_of-clamped blocks.

The backtest brief originally carried only price/technicals. Every
other block was null, which meant a replayed panel argued about a
company it could see almost nothing about, and the replays skewed
toward abstention as a result. A 2026-05-26 replay abstained citing
"fundamentals 與 revenue_trend 五檔候選股全數空值" while the archive
held snapshots for four of the five.

These tests pin the two properties that matter for the blocks now
included: they are populated, and they are clamped to `as_of`.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import services.discussion.focus_briefs as fb

_AS_OF = date(2026, 6, 4)


def _bars() -> list[dict]:
    return [
        {"time": "2026-06-02", "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.0, "volume": 1_000},
        {"time": "2026-06-03", "open": 100.0, "high": 103.0,
         "low": 99.5, "close": 102.0, "volume": 1_200},
    ]


def _inst_rows() -> list[dict]:
    return [
        {"date": "2026-06-03", "symbol": "2330",
         "fini_buy": 50_000, "fini_sell": 10_000,
         "sitc_buy": 1_000, "sitc_sell": 500,
         "dealer_buy": 200, "dealer_sell": 100},
    ]


def _margin_rows() -> list[dict]:
    return [
        {"date": "2026-06-03", "symbol": "2330",
         "margin_purchase": 5_000, "margin_balance": 20_000,
         "short_sale": 1_000, "short_balance": 3_000},
    ]


def _patches(*, inst=None, margin=None, fundamentals=None):
    """Patch every I/O edge of the backtest brief at its source module —
    the builder imports them inside the function body."""
    return [
        patch("services.ingest.repository.read_ohlcv_range_autosession",
              AsyncMock(return_value=_bars())),
        patch("services.ingest.repository.read_fundamentals_as_of_autosession",
              AsyncMock(return_value=fundamentals)),
        patch("services.ingest.repository.read_institutional_range_autosession",
              AsyncMock(return_value=inst if inst is not None else _inst_rows())),
        patch("services.ingest.repository.read_margin_range_autosession",
              AsyncMock(return_value=margin if margin is not None else _margin_rows())),
    ]


@pytest.mark.asyncio
async def test_backtest_brief_carries_chip_and_margin():
    """`chip_5d` is the core dimension of the chip_quality strategy —
    replaying that strategy without it grades a panel that cannot see
    its own thesis."""
    with (p0 := _patches())[0], p0[1], p0[2], p0[3]:
        brief = await fb._build_tw_focus_brief_backtest("2330", as_of=_AS_OF)

    assert brief["chip_5d"] is not None
    assert brief["margin_latest"] is not None
    assert brief["_backtest"] is True


@pytest.mark.asyncio
async def test_chip_reads_are_clamped_to_as_of():
    """The look-ahead boundary: neither ledger may be queried past the
    anchor, however much calendar padding the lookback adds."""
    inst_mock = AsyncMock(return_value=_inst_rows())
    margin_mock = AsyncMock(return_value=_margin_rows())
    with patch("services.ingest.repository.read_ohlcv_range_autosession",
               AsyncMock(return_value=_bars())), \
         patch("services.ingest.repository.read_fundamentals_as_of_autosession",
               AsyncMock(return_value=None)), \
         patch("services.ingest.repository.read_institutional_range_autosession",
               inst_mock), \
         patch("services.ingest.repository.read_margin_range_autosession",
               margin_mock):
        await fb._build_tw_focus_brief_backtest("2330", as_of=_AS_OF)

    for mock in (inst_mock, margin_mock):
        args = mock.await_args.args
        start, end = args[2], args[3]
        assert end == _AS_OF, "range must end on the anchor, not later"
        assert start < _AS_OF


@pytest.mark.asyncio
async def test_fundamentals_block_is_populated_when_a_snapshot_exists():
    snap = {
        "pe_ratio": 11.37, "pb_ratio": 2.69, "dividend_yield": 3.1,
        "eps": 4.2, "as_of": "2026-06-03", "data_source": "twse",
    }
    with (p0 := _patches(fundamentals=snap))[0], p0[1], p0[2], p0[3]:
        brief = await fb._build_tw_focus_brief_backtest("2330", as_of=_AS_OF)

    assert brief["fundamentals"]["pe"] == 11.37
    # The snapshot's own date is carried through so a persona can see
    # how stale it is on a session the ingest job never covered.
    assert brief["fundamentals"]["as_of"] == "2026-06-03"


@pytest.mark.asyncio
async def test_revenue_trend_stays_empty_in_backtest():
    """Deliberate, not pending: a later backfill can restate
    `revenue_yoy`, which is why the market-level reader masks it too.
    Reading it per-symbol would reintroduce that leak."""
    with (p0 := _patches())[0], p0[1], p0[2], p0[3]:
        brief = await fb._build_tw_focus_brief_backtest("2330", as_of=_AS_OF)

    assert brief["revenue_trend"] == []
    assert brief["peers"] == []


@pytest.mark.asyncio
async def test_a_failing_chip_read_degrades_instead_of_failing_the_replay():
    """A missing block costs the panel a dimension; a raised exception
    would cost the whole replayed session."""
    with patch("services.ingest.repository.read_ohlcv_range_autosession",
               AsyncMock(return_value=_bars())), \
         patch("services.ingest.repository.read_fundamentals_as_of_autosession",
               AsyncMock(return_value=None)), \
         patch("services.ingest.repository.read_institutional_range_autosession",
               AsyncMock(side_effect=RuntimeError("db down"))), \
         patch("services.ingest.repository.read_margin_range_autosession",
               AsyncMock(return_value=_margin_rows())):
        brief = await fb._build_tw_focus_brief_backtest("2330", as_of=_AS_OF)

    assert brief["chip_5d"] is None          # degraded, not fatal
    assert brief["margin_latest"] is not None
    assert brief["quote"] is not None
