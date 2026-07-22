"""Tests for tasks.auto_run_discussion.load_candidate_rows.

The TWSE feed emits no-trade sessions (suspended symbols, cash-settled
beneficiary certificates) as rows with NULL prices, so the raw bar
series is not dense. These cover the snapshot builder tolerating them.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from tasks.auto_run_discussion import load_candidate_rows


@pytest_asyncio.fixture(autouse=True)
async def _isolate_db(db_session: AsyncSession):
    await db_session.execute(delete(OhlcvDaily))
    await db_session.commit()
    yield


async def _add_bars(
    db: AsyncSession,
    symbol: str,
    count: int,
    *,
    null_close_offsets: tuple[int, ...] = (),
) -> None:
    """Insert `count` daily bars ending today; `null_close_offsets` are
    indices counted back from the newest bar (0 == newest)."""
    for i in range(count):
        blank = i in null_close_offsets
        db.add(
            OhlcvDaily(
                market="TW",
                symbol=symbol,
                ts=date(2026, 7, 15) - timedelta(days=i),
                open=None if blank else 100.0,
                high=None if blank else 101.0,
                low=None if blank else 99.0,
                close=None if blank else 100.0 + i,
                volume=0 if blank else 5_000_000,
                source="twse",
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_null_close_on_newest_bar_does_not_raise(db_session: AsyncSession):
    """A NULL-priced newest session must not blow up the whole task."""
    await _add_bars(db_session, "1312A", 30, null_close_offsets=(0,))

    rows = await load_candidate_rows(db_session)

    assert [r["symbol"] for r in rows] == ["1312A"]
    assert rows[0]["close"] == 101.0  # newest *settled* close, not the NULL row


@pytest.mark.asyncio
async def test_null_close_bars_do_not_count_toward_history(db_session: AsyncSession):
    """A symbol whose settled sessions fall under the 21-bar window is
    dropped, even when the raw row count clears it."""
    await _add_bars(db_session, "020000", 30, null_close_offsets=tuple(range(12)))

    rows = await load_candidate_rows(db_session)

    assert rows == []


@pytest.mark.asyncio
async def test_dense_series_is_unaffected(db_session: AsyncSession):
    await _add_bars(db_session, "2330", 30)

    rows = await load_candidate_rows(db_session)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "2330"
    assert rows[0]["history_days"] == 30


# ── as_of clamp (historical replay) ───────────────────────────────


@pytest.mark.asyncio
async def test_as_of_excludes_every_later_bar(db_session: AsyncSession):
    """No datum after the anchor may reach the snapshot.

    This is the whole guarantee a historical replay rests on: if a
    2026-07-01 pool can see 2026-07-15 prices, every backfilled sample
    is optimistically biased and the review built on it is worthless.
    """
    await _add_bars(db_session, "2330", 40)
    anchor = date(2026, 7, 1)

    rows = await load_candidate_rows(db_session, as_of=anchor)

    assert len(rows) == 1
    row = rows[0]
    # Bars run 2026-07-15 backwards; the newest at/​before the anchor is
    # 2026-07-01, which is offset 14 → close 100 + 14.
    assert row["close"] == 114.0
    # 40 bars end 2026-07-15, so 26 of them are on/before 2026-07-01.
    assert row["history_days"] == 26


@pytest.mark.asyncio
async def test_as_of_clamp_runs_before_the_forty_bar_trim(db_session: AsyncSession):
    """The clamp must be in SQL, not applied after the per-symbol trim.

    `load_candidate_rows` keeps only the newest 40 bars per symbol. Trim
    first and a distant anchor gets 40 bars that are all *after* it,
    every one of which then filters out — leaving the symbol under the
    21-bar minimum and the pool silently empty rather than wrong-looking.
    """
    await _add_bars(db_session, "2330", 120)
    anchor = date(2026, 7, 15) - timedelta(days=80)

    rows = await load_candidate_rows(db_session, as_of=anchor)

    assert [r["symbol"] for r in rows] == ["2330"]
    assert rows[0]["history_days"] == 40   # the trim still applies, after the clamp


@pytest.mark.asyncio
async def test_live_mode_is_unchanged_by_the_new_parameter(db_session: AsyncSession):
    await _add_bars(db_session, "2330", 30)

    assert await load_candidate_rows(db_session) == await load_candidate_rows(
        db_session, as_of=None,
    )
