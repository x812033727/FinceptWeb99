"""Tests for the TAIWAN VIX pipeline (PR #283).

Three layers:
  1. Day-file parser — `taifex_connector._parse_vix_day_body`, over
     the per-session minute file TAIFEX moved to in 2026-07
  2. Read tier — `read_tw_vix_snapshot` returns latest + 5-day
     change %; None when archive empty
  3. Upsert — bulk + ON CONFLICT idempotency
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from data.tw.taifex_connector import _parse_vix_day_body
from models.tw_vix_daily import TwVixDaily
from services.ingest.repository import (
    VixDailyRow,
    read_tw_vix_snapshot,
    upsert_tw_vix_daily,
)


# ── _parse_vix_day_body ──────────────────────────────────────────


_DAY_FILE = (
    "交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數\n"
    "--------\t-------------------\t--------------------\n"
    "20260721\t9000000\t\t\t38.13\n"
    "20260721\t9010000\t\t\t37.90\n"
    "20260721\tLast 1 min AVG\t\t\t36.55\n"
)


def test_parse_day_file_prefers_the_last_minute_average():
    """TAIFEX's own closing convention for this file is the trailing
    `Last 1 min AVG` row, not the final timestamped tick."""
    assert _parse_vix_day_body(_DAY_FILE, date(2026, 7, 21)) == pytest.approx(36.55)


def test_parse_day_file_falls_back_to_last_numeric_row():
    body = (
        "交易日期\t時間\t指數\n"
        "20260721\t9000000\t\t\t38.13\n"
        "20260721\t9010000\t\t\t37.90\n"
    )
    assert _parse_vix_day_body(body, date(2026, 7, 21)) == pytest.approx(37.90)


def test_parse_day_file_rejects_the_404_html_body():
    """Sessions outside TAIFEX's ~7-day publication window answer with
    an HTML 404 page. Parsing that as data would write garbage; parsing
    it as "no data today" would hide that we asked for something the
    endpoint no longer serves."""
    html = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">\n<html><body><h1>404</h1></body></html>'
    assert _parse_vix_day_body(html, date(2026, 6, 1)) is None


def test_parse_day_file_ignores_rows_for_another_session():
    body = _DAY_FILE + "20260720\tLast 1 min AVG\t\t\t99.99\n"
    assert _parse_vix_day_body(body, date(2026, 7, 21)) == pytest.approx(36.55)


def test_parse_day_file_empty_input():
    assert _parse_vix_day_body("", date(2026, 7, 21)) is None


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
