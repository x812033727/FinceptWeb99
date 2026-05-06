"""Tests for the Phase 1C calendar + lending batch wired into
`finmind.ingest.selfcrawl.twse`:

  - TaiwanStockTradingDate         (derive from FMTQIK rows)
  - TaiwanStockSecuritiesLending   (TWT93U — 借券賣出明細)

Both use mocked `data.tw.twse_connector` functions so tests run
without network. TaiwanStockTradingDate reuses the existing
`get_taiex_history` (already proven by `_fetch_total_return_index`)
so we don't need a fresh connector test for that path — the 1C
test exercises the wrapper-level dedup + window clip.
"""
from __future__ import annotations

from datetime import date

import pytest

from finmind.ingest.selfcrawl import covers_dataset
from finmind.ingest.selfcrawl.twse import TwseClient, supported_datasets


# ── supported_datasets pin ──────────────────────────────────────


def test_supported_datasets_includes_phase_1c_batch():
    s = supported_datasets()
    assert "TaiwanStockTradingDate" in s
    assert "TaiwanStockSecuritiesLending" in s


def test_covers_dataset_predicate_recognises_new_handlers():
    for code in ("TaiwanStockTradingDate", "TaiwanStockSecuritiesLending"):
        assert covers_dataset("twse", code) is True, code


# ── TaiwanStockTradingDate (derived from FMTQIK) ────────────────


@pytest.mark.asyncio
async def test_fetch_trading_calendar_emits_one_row_per_distinct_trading_day(
    monkeypatch,
):
    """FMTQIK returns one row per trading day for the month containing
    `query_date`. The wrapper iterates monthly chunks and dedups on
    date so a multi-month backfill emits exactly one row per
    distinct trading day. The mapping's `_row_trading_calendar`
    will tag `is_trading_day=True` regardless — wrapper just needs
    the `date` field for the column_map."""
    payloads = {
        date(2024, 1, 1): [
            {"time": "2024-01-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            {"time": "2024-01-03", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            # 2024-01-15 is a holiday — TWSE doesn't return a row
            {"time": "2024-01-31", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
        ],
        date(2024, 2, 1): [
            {"time": "2024-02-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            {"time": "2024-02-02", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
        ],
    }

    async def fake_get_taiex_history(query_date):
        return payloads.get(query_date, [])

    monkeypatch.setattr(
        "data.tw.twse_connector.get_taiex_history",
        fake_get_taiex_history,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockTradingDate", None,
        date(2024, 1, 1), date(2024, 2, 28),
    )

    # Sorted ascending; one row per distinct date.
    dates = [r["date"] for r in rows]
    assert dates == ["2024-01-02", "2024-01-03", "2024-01-31",
                     "2024-02-01", "2024-02-02"]
    # No payload columns — column_map only projects `date → ts`,
    # everything else comes from the row_transform.
    assert all(set(r.keys()) == {"date"} for r in rows)


@pytest.mark.asyncio
async def test_fetch_trading_calendar_clips_to_window(monkeypatch):
    """FMTQIK returns the WHOLE month containing query_date. The
    wrapper must clip to [start, end] so a "trading days in Jan
    2024" backfill doesn't accidentally include Feb 1 from the
    Jan call. (FMTQIK doesn't actually do this but the wrapper
    is defensive against future endpoint shape drift.)"""
    async def fake_get_taiex_history(query_date):
        if query_date == date(2024, 1, 1):
            return [
                {"time": "2024-01-15", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
                {"time": "2024-01-31", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
            ]
        return []

    monkeypatch.setattr(
        "data.tw.twse_connector.get_taiex_history",
        fake_get_taiex_history,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockTradingDate", None,
        date(2024, 1, 16), date(2024, 1, 25),  # narrow window
    )
    # 1/15 < window start, 1/31 > window end → both clipped.
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_trading_calendar_swallows_per_month_errors(monkeypatch):
    """A single TWSE 5xx for one month must not abort the whole
    chunk — the rest of the window still backfills."""
    async def fake_get_taiex_history(query_date):
        if query_date == date(2024, 2, 1):
            raise RuntimeError("simulated TWSE outage")
        return [
            {"time": f"{query_date.year}-{query_date.month:02d}-15",
             "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_taiex_history",
        fake_get_taiex_history,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockTradingDate", None,
        date(2024, 1, 1), date(2024, 3, 31),
    )
    # Jan + Mar succeed, Feb errored.
    assert {r["date"] for r in rows} == {"2024-01-15", "2024-03-15"}


# ── TaiwanStockSecuritiesLending (TWT93U) ───────────────────────


@pytest.mark.asyncio
async def test_fetch_securities_lending_translates_to_finmind_shape(
    monkeypatch,
):
    """TWT93U rows arrive with `symbol/volume/fee_rate`; the wrapper
    rewrites to FinMind's `date/stock_id/transaction_type/volume/
    fee_rate`. transaction_type is hard-coded to '借券賣出' since
    TWT93U only covers that single category — necessary so the
    `(market, symbol, ts, transaction_type)` PK doesn't get a NULL
    NOT-NULL violation."""
    async def fake_get_securities_lending_daily(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "volume": 1_000_000, "fee_rate": 0.85},
            {"symbol": "2317", "name_zh": "鴻海",
             "volume": 500_000, "fee_rate": 1.20},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_securities_lending_daily",
        fake_get_securities_lending_daily,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockSecuritiesLending", "2330",
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2024-01-02"
    assert r["stock_id"] == "2330"
    assert r["transaction_type"] == "借券賣出"
    assert r["volume"] == 1_000_000
    assert r["fee_rate"] == 0.85


@pytest.mark.asyncio
async def test_fetch_securities_lending_iterates_days(monkeypatch):
    """Range 4 days → 4 TWSE calls (TWT93U serves one date per req)."""
    seen: list = []

    async def fake_get_securities_lending_daily(query_date):
        seen.append(query_date)
        return []

    monkeypatch.setattr(
        "data.tw.twse_connector.get_securities_lending_daily",
        fake_get_securities_lending_daily,
    )

    client = TwseClient()
    await client.fetch(
        "TaiwanStockSecuritiesLending", None,
        date(2024, 1, 2), date(2024, 1, 5),
    )
    assert seen == [
        date(2024, 1, 2), date(2024, 1, 3),
        date(2024, 1, 4), date(2024, 1, 5),
    ]


@pytest.mark.asyncio
async def test_fetch_securities_lending_swallows_per_day_errors(monkeypatch):
    """A single TWSE 5xx on day N must NOT abort backfilling N±1
    within the same chunk — mirrors the Phase 1A error-handling
    contract on chip-flow handlers."""
    failures = {date(2024, 1, 3)}
    seen: list = []

    async def fake_get_securities_lending_daily(query_date):
        seen.append(query_date)
        if query_date in failures:
            raise RuntimeError("simulated TWSE 500")
        return [{"symbol": "2330", "name_zh": "台積電",
                 "volume": 1, "fee_rate": 0.85}]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_securities_lending_daily",
        fake_get_securities_lending_daily,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockSecuritiesLending", None,
        date(2024, 1, 2), date(2024, 1, 4),
    )
    assert len(seen) == 3
    # Two successes (skip the error day), each emits one row.
    assert {r["date"] for r in rows} == {"2024-01-02", "2024-01-04"}
