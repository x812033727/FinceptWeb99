"""Tests for the TAIWAN VIX pipeline (PR #283).

Three layers:
  1. Day-file parser — `taifex_connector._parse_vix_day_body`, over
     the per-session minute file TAIFEX moved to in 2026-07
  2. Read tier — `read_tw_vix_snapshot` returns latest + 5-day
     change %; None when archive empty
  3. Upsert — bulk + ON CONFLICT idempotency
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import data.tw.taifex_connector as taifex
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


# ── regime: where today's value sits in the distribution ─────────


def _seed_regime(db_session: AsyncSession, values: list[float], *, start: date):
    """One row per calendar day from `start`, in order."""
    for i, val in enumerate(values):
        db_session.add(TwVixDaily(
            market="TW", ts=start + timedelta(days=i),
            vix_value=val, source="taifex",
        ))


@pytest.mark.asyncio
async def test_regime_ranks_the_current_value_against_history(
    db_session: AsyncSession,
):
    """The bug this replaces: a hardcoded "median 16-18" made a 36.14
    print look like extreme panic when it was actually below the
    prevailing median. With 40 sessions spanning 30..40, a 36 close
    must rank mid-pack, not at the top."""
    _seed_regime(
        db_session,
        [30.0 + i * 0.25 for i in range(40)],  # 30.00 .. 39.75
        start=date(2026, 5, 1),
    )
    db_session.add(TwVixDaily(
        market="TW", ts=date(2026, 6, 20), vix_value=36.0, source="taifex",
    ))
    await db_session.commit()

    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5, as_of=date(2026, 6, 20),
    )
    assert out is not None
    regime = out["regime"]
    assert regime["sufficient"] is True
    assert regime["sample_days"] == 41
    # 36.0 sits inside the 30.00..39.75 ramp — mid-pack, not extreme.
    assert 50 <= regime["percentile"] <= 70
    assert regime["p25"] < regime["median"] < regime["p75"]


@pytest.mark.asyncio
async def test_regime_reports_insufficient_history_explicitly(
    db_session: AsyncSession,
):
    """Below the sample floor the stats are nulled but the block still
    exists — an omitted block reads to the model as "not relevant",
    which is exactly how the earlier data holes produced invented
    numbers."""
    _seed_regime(db_session, [35.0, 36.0, 37.0], start=date(2026, 6, 1))
    await db_session.commit()

    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5, as_of=date(2026, 6, 3),
    )
    assert out is not None
    regime = out["regime"]
    assert regime["sufficient"] is False
    assert regime["sample_days"] == 3
    assert regime["percentile"] is None
    assert regime["median"] is None
    assert "percentile" in regime  # present-but-null, not dropped


@pytest.mark.asyncio
async def test_regime_clamps_to_as_of_for_backtest(
    db_session: AsyncSession,
):
    """A replay must not rank its value against volatility that had not
    happened yet — the same look-ahead class as the macro block leak."""
    _seed_regime(
        db_session,
        [30.0] * 25,                       # calm pre-anchor regime
        start=date(2026, 5, 1),
    )
    _seed_regime(
        db_session,
        [90.0] * 25,                       # post-anchor panic
        start=date(2026, 7, 1),
    )
    await db_session.commit()

    out = await read_tw_vix_snapshot(
        db_session, market="TW", days=5, as_of=date(2026, 5, 25),
    )
    assert out is not None
    regime = out["regime"]
    assert regime["sample_days"] == 25          # post-anchor rows excluded
    # Ranked only against the calm regime: 30.0 is the whole sample.
    assert regime["median"] == pytest.approx(30.0)
    assert regime["p75"] == pytest.approx(30.0)


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


# ── get_vix_history: per-session walk ────────────────────────────


def _day_file(stamp: str, value: str) -> bytes:
    return (
        "交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數\n"
        f"{stamp}\t9000000\t\t\t40.00\n"
        f"{stamp}\tLast 1 min AVG\t\t\t{value}\n"
    ).encode("big5")


class _FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=None, response=None,  # type: ignore[arg-type]
            )


class _FakeClient:
    """Stands in for httpx.AsyncClient; answers per `filesname`."""

    def __init__(self, answers):
        self.answers = answers
        self.asked: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        stamp = (params or {})["filesname"]
        self.asked.append(stamp)
        answer = self.answers.get(stamp)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return _FakeResponse(b"<html><body>404</body></html>")
        return _FakeResponse(answer)


@pytest.mark.asyncio
async def test_get_vix_history_walks_weekdays_only():
    """One request per weekday: TAIFEX now publishes a file per session
    rather than a date-range download."""
    client = _FakeClient({
        "20260717": _day_file("20260717", "38.81"),
        "20260720": _day_file("20260720", "39.49"),
    })
    with patch.object(taifex.httpx, "AsyncClient", lambda **_: client):
        # 07-18 Sat, 07-19 Sun
        out = await taifex.get_vix_history(date(2026, 7, 17), date(2026, 7, 20))

    assert client.asked == ["20260717", "20260720"]
    assert out == [
        {"date": "2026-07-17", "value": pytest.approx(38.81)},
        {"date": "2026-07-20", "value": pytest.approx(39.49)},
    ]


@pytest.mark.asyncio
async def test_get_vix_history_skips_unpublished_sessions():
    """Dates outside the ~7-session window answer with an HTML 404 page;
    they are skipped, not parsed and not fatal."""
    client = _FakeClient({
        "20260720": None,                             # HTML 404 body
        "20260721": _day_file("20260721", "36.55"),
    })
    with patch.object(taifex.httpx, "AsyncClient", lambda **_: client):
        out = await taifex.get_vix_history(date(2026, 7, 20), date(2026, 7, 21))

    assert out == [{"date": "2026-07-21", "value": pytest.approx(36.55)}]


@pytest.mark.asyncio
async def test_get_vix_history_one_transport_error_does_not_lose_the_rest():
    client = _FakeClient({
        "20260720": httpx.ConnectTimeout("down"),
        "20260721": _day_file("20260721", "36.55"),
    })
    with patch.object(taifex.httpx, "AsyncClient", lambda **_: client):
        out = await taifex.get_vix_history(date(2026, 7, 20), date(2026, 7, 21))

    assert out == [{"date": "2026-07-21", "value": pytest.approx(36.55)}]


@pytest.mark.asyncio
async def test_get_vix_history_inverted_range_is_empty():
    out = await taifex.get_vix_history(date(2026, 7, 21), date(2026, 7, 20))
    assert out == []


# ── monthly archive: the only free source of VIX history ─────────
#
# The per-session endpoint above only publishes ~7 sessions, which
# left `tw_vix_daily` with 15 rows — too few to say whether a given
# VIX level is high or low for the current regime. TAIFEX also
# publishes one file per month (4 months retained), which is where
# the distribution comes from.


_MONTH_FILE = (
    "交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數\t收盤前1分鐘平均指數\n"
    "--------\t-------------------\t--------------------\t-------------------\n"
    "20260701\t13450000\t\t\t38.12\t\t38.14\n"
    "20260702\t13450000\t\t\t37.82\t\t37.76\n"
    "20260703\t13450000\t\t\t36.61\t\t36.58\n"
)


def test_parse_month_file_takes_the_closing_minute_average():
    """The month file carries BOTH the raw close (col 3) and the
    closing-minute average (col 4). The per-session file's
    `Last 1 min AVG` — what `tw_vix_daily` already stores — is the
    latter, so the two sources must agree on column 4 or the archive
    would mix two different conventions."""
    out = taifex._parse_vix_month_body(_MONTH_FILE)
    assert out == [
        {"date": "2026-07-01", "value": pytest.approx(38.14)},
        {"date": "2026-07-02", "value": pytest.approx(37.76)},
        {"date": "2026-07-03", "value": pytest.approx(36.58)},
    ]


def test_parse_month_file_rejects_the_404_html_body():
    """Months outside the 4-month retention window 302 to an HTML
    404 page — must not be parsed as data."""
    html = '<!DOCTYPE HTML><html><body><h1>404</h1></body></html>'
    assert taifex._parse_vix_month_body(html) == []


def test_parse_month_file_skips_malformed_rows():
    body = _MONTH_FILE + "notadate\t13450000\t\t\t1.0\t\t2.0\nblah\n"
    out = taifex._parse_vix_month_body(body)
    assert len(out) == 3


class _FakeMonthClient:
    """Answers per month-file URL rather than per `filesname` param."""

    def __init__(self, answers: dict[str, bytes | None | Exception]):
        self.answers = answers
        self.asked: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        stamp = url.rsplit("/", 1)[-1].replace("new.txt", "")
        self.asked.append(stamp)
        answer = self.answers.get(stamp)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return _FakeResponse(b"<html><body>404</body></html>")
        return _FakeResponse(answer)


def _month_file(rows: list[tuple[str, str]]) -> bytes:
    head = (
        "交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數\t收盤前1分鐘平均指數\n"
        "--------\t-------------------\t--------------------\t-------------------\n"
    )
    body = "".join(
        f"{stamp}\t13450000\t\t\t99.99\t\t{value}\n" for stamp, value in rows
    )
    return (head + body).encode("big5")


@pytest.mark.asyncio
async def test_get_vix_monthly_history_walks_back_n_months():
    client = _FakeMonthClient({
        "202607": _month_file([("20260701", "38.14")]),
        "202606": _month_file([("20260601", "35.10")]),
    })
    with patch.object(taifex.httpx, "AsyncClient", lambda **_: client):
        out = await taifex.get_vix_monthly_history(
            months=2, end=date(2026, 7, 23),
        )

    # Newest month requested first, output sorted oldest → newest.
    assert client.asked == ["202607", "202606"]
    assert out == [
        {"date": "2026-06-01", "value": pytest.approx(35.10)},
        {"date": "2026-07-01", "value": pytest.approx(38.14)},
    ]


@pytest.mark.asyncio
async def test_get_vix_monthly_history_crosses_the_year_boundary():
    client = _FakeMonthClient({
        "202601": _month_file([("20260105", "30.00")]),
        "202512": _month_file([("20251203", "28.00")]),
    })
    with patch.object(taifex.httpx, "AsyncClient", lambda **_: client):
        out = await taifex.get_vix_monthly_history(
            months=2, end=date(2026, 1, 20),
        )

    assert client.asked == ["202601", "202512"]
    assert [r["date"] for r in out] == ["2025-12-03", "2026-01-05"]


@pytest.mark.asyncio
async def test_get_vix_monthly_history_one_missing_month_keeps_the_rest():
    """Older months fall out of TAIFEX's retention window. That is the
    normal steady state, not a failure — but a month that answers with
    nothing must not silently look like a month with no trading days."""
    client = _FakeMonthClient({
        "202607": _month_file([("20260701", "38.14")]),
        "202606": None,                                   # aged out
        "202605": httpx.ConnectTimeout("down"),           # transport
    })
    with patch.object(taifex.httpx, "AsyncClient", lambda **_: client):
        out = await taifex.get_vix_monthly_history(
            months=3, end=date(2026, 7, 23),
        )

    assert out == [{"date": "2026-07-01", "value": pytest.approx(38.14)}]
