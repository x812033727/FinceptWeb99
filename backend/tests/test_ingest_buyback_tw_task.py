"""Tests for `tasks.ingest_buyback_tw` — daily TW buyback ingest.

Source is MOPS' 庫藏股統計彙總表 (`mops_connector.get_buyback_summary`)
since 2026-07-28: FinMind removed `TaiwanStockBuyBack` (HTTP 422) and
TWSE re-assigned OpenAPI `t187ap43_L` to warrant data, so the FinMind
paywall/422 fail-soft branches are gone with the FinMind call. Covers:
lock-held short-circuit, success path that writes rows + clears
failures, outage → backoff, scheduler registration, and the
re-pull-overwrites idempotency of the repository helpers.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_stock_buyback import TwStockBuyback


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_buyback_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _row(symbol: str = "2330", date_str: str = "2026-04-15", **overrides) -> dict:
    """Mimics the normalized shape `mops_connector.get_buyback_summary`
    returns — keys match what `_do_run` consumes."""
    return {
        "symbol": symbol,
        "name_zh": "台積電",
        "announce_date": date_str,
        "method": 3,
        "purpose": "維護公司信用及股東權益",
        "period_start": "2026-04-15",
        "period_end": "2026-06-15",
        "max_shares": 1_000_000,
        "current_shares": 250_000,
        "price_lower": 800.0,
        "price_upper": 1200.0,
        **overrides,
    }


def _http_status_error(status_code: int, body_msg: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://mops.twse.com.tw/mops/api/redirectToOld")
    response = httpx.Response(
        status_code, request=request,
        json={"message": body_msg, "code": status_code},
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


# ── happy path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_buyback_tw
    fetch_mock = AsyncMock()
    with patch("tasks.ingest_buyback_tw.acquire_lock",
               AsyncMock(return_value=False)), \
         patch("tasks.ingest_buyback_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.get_buyback_summary", fetch_mock):
        await ingest_buyback_tw.run()
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_success_writes_rows(
    patch_session, db_session: AsyncSession,
):
    """End-to-end happy path: connector returns a list, cron upserts
    into tw_stock_buyback, health row records ok=True."""
    from tasks import ingest_buyback_tw

    health_mock = AsyncMock()
    payload = [
        _row(symbol="2330", date_str="2026-04-15"),
        _row(symbol="2454", date_str="2026-04-20",
             max_shares=500_000, current_shares=0,
             price_lower=900.0, price_upper=1300.0),
    ]
    with patch("tasks.ingest_buyback_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_buyback_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_buyback_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.record_health", health_mock), \
         patch("tasks.ingest_buyback_tw.get_buyback_summary",
               AsyncMock(return_value=payload)):
        await ingest_buyback_tw.run()

    db_rows = (await db_session.scalars(
        select(TwStockBuyback).order_by(TwStockBuyback.symbol)
    )).all()
    assert len(db_rows) == 2
    by_sym = {r.symbol: r for r in db_rows}
    assert by_sym["2330"].announce_date == date(2026, 4, 15)
    assert int(by_sym["2330"].max_shares) == 1_000_000
    assert int(by_sym["2330"].current_shares) == 250_000
    assert by_sym["2330"].purpose == "維護公司信用及股東權益"
    assert by_sym["2454"].method == 3
    assert by_sym["2454"].source == "mops"

    health_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_skips_rows_with_unparseable_date(
    patch_session, db_session: AsyncSession,
):
    """Defensive: a single bad announce_date doesn't poison the
    whole batch."""
    from tasks import ingest_buyback_tw
    payload = [
        _row(symbol="2330", date_str="2026-04-15"),
        _row(symbol="2454", date_str=""),     # blank date — drop
        _row(symbol="9999", date_str="bad"),  # unparseable — drop
    ]
    with patch("tasks.ingest_buyback_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_buyback_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_buyback_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.get_buyback_summary",
               AsyncMock(return_value=payload)):
        await ingest_buyback_tw.run()

    db_rows = (await db_session.scalars(select(TwStockBuyback))).all()
    assert {r.symbol for r in db_rows} == {"2330"}


# ── failure regime ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_genuine_outage_arms_backoff(patch_session):
    from tasks import ingest_buyback_tw
    record_failure_mock = AsyncMock(return_value=1)
    clear_failures_mock = AsyncMock()
    health_mock = AsyncMock()
    with patch("tasks.ingest_buyback_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_buyback_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_buyback_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_buyback_tw.record_failure", record_failure_mock), \
         patch("tasks.ingest_buyback_tw.clear_failures", clear_failures_mock), \
         patch("tasks.ingest_buyback_tw.record_health", health_mock), \
         patch("tasks.ingest_buyback_tw.get_buyback_summary",
               AsyncMock(side_effect=_http_status_error(503, "MOPS unavailable"))):
        await ingest_buyback_tw.run()

    record_failure_mock.assert_awaited_once()
    clear_failures_mock.assert_not_called()
    kwargs = health_mock.await_args.kwargs
    assert "auto-backoff armed" in kwargs["error"]
    assert "skipped" not in kwargs["error"].lower()


def test_scheduler_registers_the_cron():
    """Revived 2026-07-28 — 18:10 Taipei (10:10 UTC), after the
    post-close cluster."""
    from tasks.scheduler import scheduler, setup_jobs

    with patch.object(scheduler, "add_job") as add_job:
        setup_jobs()
    jobs = {
        call.kwargs["id"]: call
        for call in add_job.call_args_list if "id" in call.kwargs
    }
    assert "ingest_buyback_tw" in jobs
    trigger = jobs["ingest_buyback_tw"].kwargs["trigger"]
    trig = str(trigger)
    assert "hour='10'" in trig and "minute='10'" in trig, trig
    assert str(trigger.timezone) == "UTC"


# ── repository helpers ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_active_buybacks_filters_by_period_end(
    db_session: AsyncSession,
):
    """`read_active_buybacks` returns rows whose period_end is in
    the future (or NULL — defensive for fresh announcements where
    the end date hasn't been published yet). Past rows excluded."""
    from services.ingest.repository import (
        BuybackRow, read_active_buybacks, upsert_buybacks,
    )
    today = date(2026, 5, 1)
    await upsert_buybacks(db_session, [
        # active — period covers today
        BuybackRow(market="TW", symbol="2330",
                   announce_date=date(2026, 4, 15),
                   period_start=date(2026, 4, 15),
                   period_end=date(2026, 6, 15),
                   method=1, purpose="維護股東權益",
                   max_shares=1_000_000, current_shares=250_000,
                   price_lower=800.0, price_upper=1200.0,
                   source="mops"),
        # expired — period_end in the past
        BuybackRow(market="TW", symbol="2317",
                   announce_date=date(2025, 1, 1),
                   period_start=date(2025, 1, 1),
                   period_end=date(2025, 3, 1),
                   method=1, purpose="維護股東權益",
                   max_shares=500_000, current_shares=500_000,
                   price_lower=100.0, price_upper=120.0,
                   source="mops"),
        # NULL period_end — treated as still-open
        BuybackRow(market="TW", symbol="2454",
                   announce_date=date(2026, 4, 28),
                   period_start=date(2026, 4, 28),
                   period_end=None,
                   method=1, purpose=None,
                   max_shares=200_000, current_shares=0,
                   price_lower=None, price_upper=None,
                   source="mops"),
    ])

    rows = await read_active_buybacks(db_session, market="TW", as_of=today)
    syms = {r["symbol"] for r in rows}
    assert syms == {"2330", "2454"}


@pytest.mark.asyncio
async def test_upsert_buyback_overwrites_current_shares_on_repull(
    db_session: AsyncSession,
):
    """Mid-window re-pull updates `current_shares` (execution
    progress) without creating a duplicate row."""
    from services.ingest.repository import BuybackRow, upsert_buybacks
    base = BuybackRow(
        market="TW", symbol="2330",
        announce_date=date(2026, 4, 15),
        period_start=date(2026, 4, 15),
        period_end=date(2026, 6, 15),
        method=1, purpose="維護股東權益",
        max_shares=1_000_000, current_shares=250_000,
        price_lower=800.0, price_upper=1200.0,
        source="mops",
    )
    await upsert_buybacks(db_session, [base])
    # Day later — execution advanced.
    await upsert_buybacks(db_session, [
        BuybackRow(**{**base.__dict__, "current_shares": 480_000}),
    ])

    db_rows = (await db_session.scalars(
        select(TwStockBuyback).where(TwStockBuyback.symbol == "2330")
    )).all()
    assert len(db_rows) == 1
    assert int(db_rows[0].current_shares) == 480_000
