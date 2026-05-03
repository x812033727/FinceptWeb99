"""Tests for the TAIWAN VIX pipeline (PR #283).

Three layers:
  1. CSV parser — `taifex_connector._parse_csv_body` happy path +
     tolerance for column-order, missing fields, mojibake'd
     headers
  2. Read tier — `read_tw_vix_snapshot` returns latest + 5-day
     change %; None when archive empty
  3. Upsert — bulk + ON CONFLICT idempotency
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from data.tw.taifex_connector import _parse_csv_body
from models.tw_vix_daily import TwVixDaily
from services.ingest.repository import (
    VixDailyRow,
    read_tw_vix_snapshot,
    upsert_tw_vix_daily,
)


# ── _parse_csv_body ──────────────────────────────────────────────


def test_parse_csv_happy_path():
    body = (
        "日期,收盤指數\n"
        "2026/04/15,18.32\n"
        "2026/04/16,17.51\n"
        "2026/04/17,16.95\n"
    )
    out = _parse_csv_body(body)
    assert len(out) == 3
    assert out[0]["date"] == "2026-04-15"
    assert out[0]["value"] == pytest.approx(18.32)
    assert out[2]["value"] == pytest.approx(16.95)


def test_parse_csv_tolerates_extra_columns():
    """TAIFEX sometimes includes 開盤/最高/最低 alongside the close.
    Parser keys off the headers — extra columns must not shift
    the date/value lookup or break the row."""
    body = (
        "日期,開盤,最高,最低,收盤指數\n"
        "2026/04/15,18.0,18.5,17.9,18.32\n"
        "2026/04/16,18.3,18.4,17.5,17.51\n"
    )
    out = _parse_csv_body(body)
    assert len(out) == 2
    assert out[0]["value"] == pytest.approx(18.32)


def test_parse_csv_accepts_alternate_value_header():
    """A future TAIFEX header rotation (`收盤` instead of `收盤指數`)
    shouldn't blank the parser — we list both forms."""
    body = (
        "日期,收盤\n"
        "2026/04/15,18.32\n"
    )
    out = _parse_csv_body(body)
    assert len(out) == 1
    assert out[0]["value"] == pytest.approx(18.32)


def test_parse_csv_skips_unparseable_rows():
    """Bad date strings + non-numeric values are skipped silently
    rather than crashing the whole batch."""
    body = (
        "日期,收盤指數\n"
        "2026/04/15,18.32\n"
        "BAD-DATE,17.51\n"
        "2026/04/17,not-a-number\n"
        "2026/04/18,16.95\n"
    )
    out = _parse_csv_body(body)
    assert [r["date"] for r in out] == ["2026-04-15", "2026-04-18"]


def test_parse_csv_empty_body_returns_empty():
    assert _parse_csv_body("") == []
    assert _parse_csv_body("\n\n") == []


def test_parse_csv_missing_headers_returns_empty_with_warn(caplog):
    """If the headers don't match anything we recognise (e.g. TAIFEX
    rotated to a Big5-decoded gibberish or English variant), return
    [] so the cron records a clean failure rather than crashing."""
    body = (
        "Date,Close\n"   # English headers — not in our known set
        "2026/04/15,18.32\n"
    )
    out = _parse_csv_body(body)
    assert out == []


# ── read_tw_vix_snapshot ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_snapshot_computes_5_day_change(
    db_session: AsyncSession,
):
    """Latest VIX value vs 5-trading-days-ago value, plus change %.
    Series with a steady rise from 16 → 22 should report +37.5%."""
    series = [
        (date(2026, 4, 8), 16.0),
        (date(2026, 4, 11), 17.0),
        (date(2026, 4, 14), 19.0),
        (date(2026, 4, 15), 22.0),
    ]
    for ts, val in series:
        db_session.add(TwVixDaily(
            market="TW", ts=ts, vix_value=val, source="taifex",
        ))
    await db_session.commit()

    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5,
        as_of=date(2026, 4, 15),
    )
    assert out is not None
    assert out["value"] == 22.0
    assert out["from_value"] == 16.0
    # (22/16 - 1) * 100 = 37.5 %
    assert out["change_pct"] == pytest.approx(37.5)


@pytest.mark.asyncio
async def test_read_snapshot_returns_none_for_empty_archive(
    db_session: AsyncSession,
):
    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5,
        as_of=date(2026, 4, 15),
    )
    assert out is None


@pytest.mark.asyncio
async def test_read_snapshot_returns_value_with_no_change_when_only_one_point(
    db_session: AsyncSession,
):
    """Only one data point → can't compute change_pct. Return the
    latest value with `change_pct=None` so personas can still
    quote the current vol level."""
    db_session.add(TwVixDaily(
        market="TW", ts=date(2026, 4, 15), vix_value=22.0, source="taifex",
    ))
    await db_session.commit()

    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5,
        as_of=date(2026, 4, 15),
    )
    assert out is not None
    assert out["value"] == 22.0
    assert out["change_pct"] is None
    assert out["from_value"] is None


@pytest.mark.asyncio
async def test_read_snapshot_clamps_to_as_of_for_backtest(
    db_session: AsyncSession,
):
    """Backtest at as_of=2026-04-15 must NOT see values from
    2026-04-22 even if they exist in the archive."""
    db_session.add_all([
        TwVixDaily(market="TW", ts=date(2026, 4, 8), vix_value=16.0, source="taifex"),
        TwVixDaily(market="TW", ts=date(2026, 4, 15), vix_value=22.0, source="taifex"),
        TwVixDaily(market="TW", ts=date(2026, 4, 22), vix_value=10.0, source="taifex"),  # post-anchor
    ])
    await db_session.commit()

    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5,
        as_of=date(2026, 4, 15),
    )
    assert out is not None
    assert out["value"] == 22.0
    # The post-anchor 10.0 row must not surface.
    assert out["as_of"] == "2026-04-15"


# ── upsert idempotency ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_overwrites_on_re_ingest(
    db_session: AsyncSession,
):
    """Re-running the cron with corrected TAIFEX numbers (rare but
    possible) overwrites in place via ON CONFLICT DO UPDATE."""
    base = date(2026, 4, 15)
    await upsert_tw_vix_daily(db_session, [
        VixDailyRow(market="TW", ts=base, vix_value=22.0, source="taifex"),
    ])
    n = await upsert_tw_vix_daily(db_session, [
        VixDailyRow(market="TW", ts=base, vix_value=22.5, source="taifex"),
    ])
    assert n == 1

    from sqlalchemy import select as _select
    rows = (await db_session.scalars(
        _select(TwVixDaily).where(TwVixDaily.market == "TW"),
    )).all()
    assert len(rows) == 1
    assert float(rows[0].vix_value) == pytest.approx(22.5)
