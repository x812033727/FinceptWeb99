"""Dated margin reads — the legacy MI_MARGN surface.

`ingest_margin_tw` reads the OpenAPI snapshot, which takes no date:
passing one is silently ignored and answers with today's numbers. A
backfill through that surface would stamp today's balances onto
historical rows, so the dated path goes to the legacy site instead.

The parsing risk here is the header: 買進 / 賣出 / 前日餘額 /
今日餘額 / 次一營業日限額 each appear TWICE in the per-symbol table,
once for margin and once for short. Zipping names to values lets the
short columns overwrite the margin ones, so the columns are read by
index and these tests pin those indices.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import data.tw.twse_connector as twse

# Real shape, taken from 2026-06-04 (2330 台積電).
_FIELDS = [
    "代號", "名稱",
    "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
    "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額",
    "資券互抵", "註記",
]
_ROW_2330 = [
    "2330", "台積電",
    "950", "849", "23", "28,310", "28,388", "6,483,131",
    "18", "0", "0", "104", "86", "6,483,131",
    "1", "X",
]


def _envelope(rows: list[list[str]], stat: str = "OK") -> dict:
    return {
        "stat": stat,
        "date": "20260604",
        "tables": [
            {"title": "信用交易統計", "fields": ["項目"], "data": [["融資(交易單位)"]]},
            {"title": "融資融券彙總 (全部)", "fields": _FIELDS, "data": rows},
        ],
    }


def test_positional_parse_keeps_margin_and_short_apart():
    """The header repeats, so a name-keyed parse would report the short
    balance as the margin balance. 2330 on 2026-06-04: margin balance
    28,388 and short balance 86 — they must not collapse."""
    out = twse._parse_legacy_margin(_envelope([_ROW_2330]))
    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "2330"
    assert row["margin_purchase"] == 950
    assert row["margin_balance"] == 28_388
    assert row["short_sale"] == 0
    assert row["short_balance"] == 86


def test_parse_skips_short_and_malformed_rows():
    out = twse._parse_legacy_margin(_envelope([
        _ROW_2330,
        ["2317", "鴻海"],            # truncated
        ["", "沒有代號", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"],
        "not-a-row",                 # type: ignore[list-item]
    ]))
    assert [r["symbol"] for r in out] == ["2330"]


def test_parse_returns_empty_on_non_ok_stat():
    """Weekends and holidays answer with a non-OK envelope. Empty is
    'no session', which the backfill counts separately from a failure."""
    assert twse._parse_legacy_margin(_envelope([_ROW_2330], stat="很抱歉，沒有符合條件的資料!")) == []


def test_parse_returns_empty_when_symbol_table_missing():
    payload = {"stat": "OK", "tables": [{"fields": ["項目"], "data": []}]}
    assert twse._parse_legacy_margin(payload) == []
    assert twse._parse_legacy_margin({"stat": "OK"}) == []
    assert twse._parse_legacy_margin(None) == []


@pytest.mark.asyncio
async def test_dated_call_goes_to_the_legacy_surface():
    """The OpenAPI variant ignores a date, so the dated path must not
    use it — otherwise every backfilled session would carry today's
    balances."""
    got = {}

    async def _fake_get(url, params=None):
        got["url"] = url
        got["params"] = params
        return _envelope([_ROW_2330])

    with patch.object(twse, "_get", AsyncMock(side_effect=_fake_get)):
        out = await twse.get_margin(date(2026, 6, 4))

    assert "www.twse.com.tw" in got["url"]
    assert got["params"]["date"] == "20260604"
    assert got["params"]["selectType"] == "ALL"
    assert out[0]["margin_balance"] == 28_388


@pytest.mark.asyncio
async def test_undated_call_still_uses_the_openapi_snapshot():
    """The daily cron path is unchanged — it wants today and the
    OpenAPI shape is a flat list of dicts."""
    got = {}

    async def _fake_get(url, params=None):
        got["url"] = url
        got["params"] = params
        return [{
            "股票代號": "2330", "名稱": "台積電",
            "融資買進": "950", "融資餘額": "28,388",
            "融券賣出": "0", "融券餘額": "86",
        }]

    with patch.object(twse, "_get", AsyncMock(side_effect=_fake_get)):
        out = await twse.get_margin()

    assert "openapi.twse.com.tw" in got["url"]
    assert "date" not in got["params"]
    assert out[0]["margin_balance"] == 28_388
