"""Tests for the Phase 1B corporate-action batch wired into
`finmind.ingest.selfcrawl.twse`:

  - TaiwanStockBuyBack    (t187ap43_L — 庫藏股公告)
  - TaiwanStockDelisting  (t187ap08_L — 終止上市)

Both endpoints are one-shot snapshots — TWSE returns ALL recent
announcements / delistings in one call rather than per-day cursors,
so the wrappers iterate the snapshot in-process and clip by
announcement / delisting date. We mock the underlying connector
functions so tests run without network.
"""
from __future__ import annotations

from datetime import date

import pytest

from finmind.ingest.selfcrawl import covers_dataset
from finmind.ingest.selfcrawl.twse import TwseClient, supported_datasets


# ── supported_datasets pin ──────────────────────────────────────


def test_supported_datasets_includes_phase_1b_batch():
    s = supported_datasets()
    assert "TaiwanStockBuyBack" in s
    assert "TaiwanStockDelisting" in s


def test_covers_dataset_predicate_recognises_new_handlers():
    for code in ("TaiwanStockBuyBack", "TaiwanStockDelisting"):
        assert covers_dataset("twse", code) is True, code


# ── TaiwanStockBuyBack (t187ap43_L) ─────────────────────────────


@pytest.mark.asyncio
async def test_fetch_buyback_translates_to_finmind_shape(monkeypatch):
    """Connector returns rows with stable English keys + ISO dates;
    wrapper rewrites to FinMind's TaiwanStockBuyBack column_map keys
    (date / stock_id / BuyBackStartDate / ...). One HTTP per chunk
    regardless of date range — t187ap43_L is one-shot."""
    fetched_count = 0

    async def fake_get_buyback_announcements():
        nonlocal fetched_count
        fetched_count += 1
        return [
            {
                "symbol":        "2330", "name_zh": "台積電",
                "announced_at":  "2024-04-15",
                "started_at":    "2024-04-16",
                "ended_at":      "2024-06-15",
                "plan_shares":   10_000_000,
                "actual_shares": 5_000_000,
                "avg_price":     850.0,
            },
            {
                "symbol":        "2317", "name_zh": "鴻海",
                "announced_at":  "2024-05-02",
                "started_at":    "2024-05-03",
                "ended_at":      "2024-07-02",
                "plan_shares":   1_000_000,
                "actual_shares": 100_000,
                "avg_price":     150.0,
            },
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_buyback_announcements",
        fake_get_buyback_announcements,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockBuyBack", None,
        date(2024, 4, 1), date(2024, 6, 30),
    )

    assert fetched_count == 1  # one-shot: not per-day fan-out
    assert len(rows) == 2
    by_id = {r["stock_id"]: r for r in rows}
    r = by_id["2330"]
    assert r["date"] == "2024-04-15"
    assert r["BuyBackStartDate"] == "2024-04-16"
    assert r["BuyBackEndDate"] == "2024-06-15"
    assert r["BuyBackPlanQuantity"] == 10_000_000
    assert r["BuyBackActualQuantity"] == 5_000_000
    assert r["BuyBackAveragePrice"] == 850.0


@pytest.mark.asyncio
async def test_fetch_buyback_clips_by_announced_at_window(monkeypatch):
    """Date filter is on `announced_at` (PK alongside symbol). Out-of-
    window rows must drop so a "January 2024 buybacks" backfill
    doesn't accidentally ingest an unrelated 2023 announcement that
    happens to still appear in the rolling t187ap43_L snapshot."""
    async def fake_get_buyback_announcements():
        return [
            {"symbol": "1101", "name_zh": "台泥",
             "announced_at": "2023-12-30",  # before window
             "started_at": "2024-01-02", "ended_at": "2024-03-01",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
            {"symbol": "2330", "name_zh": "台積電",
             "announced_at": "2024-01-15",  # in window
             "started_at": "2024-01-16", "ended_at": "2024-03-15",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
            {"symbol": "2317", "name_zh": "鴻海",
             "announced_at": "2024-02-05",  # in window
             "started_at": "2024-02-06", "ended_at": "2024-04-05",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
            {"symbol": "2454", "name_zh": "聯發科",
             "announced_at": "2024-03-10",  # after window
             "started_at": "2024-03-11", "ended_at": "2024-05-10",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_buyback_announcements",
        fake_get_buyback_announcements,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockBuyBack", None,
        date(2024, 1, 1), date(2024, 2, 28),
    )

    assert {r["stock_id"] for r in rows} == {"2330", "2317"}


@pytest.mark.asyncio
async def test_fetch_buyback_filters_by_symbol(monkeypatch):
    async def fake_get_buyback_announcements():
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "announced_at": "2024-04-15",
             "started_at": "2024-04-16", "ended_at": "2024-06-15",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
            {"symbol": "2317", "name_zh": "鴻海",
             "announced_at": "2024-05-02",
             "started_at": "2024-05-03", "ended_at": "2024-07-02",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_buyback_announcements",
        fake_get_buyback_announcements,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockBuyBack", "2330",
        date(2024, 1, 1), date(2024, 12, 31),
    )
    assert len(rows) == 1
    assert rows[0]["stock_id"] == "2330"


@pytest.mark.asyncio
async def test_fetch_buyback_drops_rows_with_unparseable_announced_at(
    monkeypatch,
):
    """A row whose `announced_at` is None (TWSE rotation broke the
    date column) must drop rather than poison the chunk."""
    async def fake_get_buyback_announcements():
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "announced_at": None,  # connector returned unparseable date
             "started_at": "2024-04-16", "ended_at": "2024-06-15",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
            {"symbol": "2317", "name_zh": "鴻海",
             "announced_at": "2024-05-02",
             "started_at": "2024-05-03", "ended_at": "2024-07-02",
             "plan_shares": 1, "actual_shares": 0, "avg_price": 0.0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_buyback_announcements",
        fake_get_buyback_announcements,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockBuyBack", None,
        date(2024, 1, 1), date(2024, 12, 31),
    )
    assert {r["stock_id"] for r in rows} == {"2317"}


# ── TaiwanStockDelisting (t187ap08_L) ───────────────────────────


@pytest.mark.asyncio
async def test_fetch_delisting_translates_to_finmind_shape(monkeypatch):
    """Connector returns symbol/name_zh/delisted_at/reason; wrapper
    rewrites to FinMind's stock_id/date/reason so the column_map
    projects onto tw_delisting unchanged."""
    async def fake_get_delisted_companies():
        return [
            {"symbol": "2520", "name_zh": "冠德",
             "delisted_at": "2023-08-22", "reason": "申請終止上市"},
            {"symbol": "1456", "name_zh": "怡華",
             "delisted_at": "2024-03-15", "reason": "下市"},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_delisted_companies",
        fake_get_delisted_companies,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockDelisting", None,
        date(2023, 1, 1), date(2024, 12, 31),
    )
    assert len(rows) == 2
    by_id = {r["stock_id"]: r for r in rows}
    assert by_id["2520"]["date"] == "2023-08-22"
    assert by_id["2520"]["reason"] == "申請終止上市"
    assert by_id["1456"]["date"] == "2024-03-15"


@pytest.mark.asyncio
async def test_fetch_delisting_clips_by_window(monkeypatch):
    """t187ap08_L is the master list (every delisting on file ~2010+).
    The wrapper must clip to [start, end] so a "delistings in 2024"
    backfill doesn't ingest historic 2015 rows."""
    async def fake_get_delisted_companies():
        return [
            {"symbol": "1234", "name_zh": "舊", "delisted_at": "2015-06-01", "reason": "歷史"},
            {"symbol": "2520", "name_zh": "冠德", "delisted_at": "2024-03-15", "reason": "近期"},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_delisted_companies",
        fake_get_delisted_companies,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockDelisting", None,
        date(2024, 1, 1), date(2024, 12, 31),
    )
    assert {r["stock_id"] for r in rows} == {"2520"}


@pytest.mark.asyncio
async def test_fetch_delisting_filters_by_symbol(monkeypatch):
    async def fake_get_delisted_companies():
        return [
            {"symbol": "2520", "name_zh": "冠德",
             "delisted_at": "2024-03-15", "reason": "下市"},
            {"symbol": "1456", "name_zh": "怡華",
             "delisted_at": "2024-04-01", "reason": "下市"},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_delisted_companies",
        fake_get_delisted_companies,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockDelisting", "2520",
        date(2024, 1, 1), date(2024, 12, 31),
    )
    assert len(rows) == 1
    assert rows[0]["stock_id"] == "2520"
