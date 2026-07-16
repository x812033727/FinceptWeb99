"""Tests for `tasks.ingest_govt_bank_flow_tw` — 八大行庫 daily flow."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_govt_bank_flow import TwGovtBankFlowDaily


def _market_wide_stub(payload: list[dict]):
    """FinMind's market-wide mode answers with one day's rows, and the
    job now asks once per day in its window. A stub that returns the
    whole payload for every date would let each row be counted once per
    call — so serve each date only its own rows, as the API does.
    """
    async def _fetch(day_iso: str) -> list[dict]:
        return [r for r in payload if str(r.get("date", ""))[:10] == day_iso]

    return _fetch


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_govt_bank_flow_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _row(date_str: str, bank: str, buy: int, sell: int) -> dict:
    return {
        "date": date_str, "bank_name": bank,
        "buy_amount": buy, "sell_amount": sell,
    }


def _recent_weekdays(count: int) -> list[date]:
    """The last `count` weekdays, oldest first. The job only asks for
    days inside `today - _LOOKBACK_DAYS`, so fixtures have to sit in that
    window rather than on a frozen calendar date."""
    days: list[date] = []
    day = date.today()
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def _http_status_error(status_code: int, body_msg: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    response = httpx.Response(
        status_code, request=request,
        json={"msg": body_msg, "status": status_code},
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


@pytest.mark.asyncio
async def test_success_writes_per_bank_rows(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_govt_bank_flow_tw

    day_one, day_two = _recent_weekdays(2)
    payload = [
        _row(day_one.isoformat(), "臺灣銀行", 5_000_000_000, 3_000_000_000),
        _row(day_one.isoformat(), "兆豐國際", 2_000_000_000, 4_000_000_000),
        _row(day_two.isoformat(), "臺灣銀行", 6_000_000_000, 2_500_000_000),
    ]
    with patch("tasks.ingest_govt_bank_flow_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_govt_bank_flow_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_govt_bank_flow_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.finmind.get_government_bank_flow_market_wide",
               _market_wide_stub(payload)):
        await ingest_govt_bank_flow_tw.run()

    rows = (await db_session.scalars(select(TwGovtBankFlowDaily))).all()
    assert len(rows) == 3
    by_key = {(r.ts, r.bank_name): r for r in rows}
    assert int(by_key[(day_one, "臺灣銀行")].buy_amount) == 5_000_000_000
    assert int(by_key[(day_one, "兆豐國際")].sell_amount) == 4_000_000_000


@pytest.mark.asyncio
async def test_asks_for_every_weekday_in_the_window_not_just_the_far_end(
    patch_session, db_session: AsyncSession,
):
    """The market-wide endpoint answers with one day and ignores
    end_date, so passing `today - lookback` archived exactly that one
    stale day and left the table frozen there."""
    from tasks import ingest_govt_bank_flow_tw

    asked: list[str] = []

    async def _fetch(day_iso: str) -> list[dict]:
        asked.append(day_iso)
        return []

    with patch("tasks.ingest_govt_bank_flow_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_govt_bank_flow_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_govt_bank_flow_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.finmind.get_government_bank_flow_market_wide",
               _fetch):
        await ingest_govt_bank_flow_tw.run()

    assert date.today().isoformat() in asked or date.today().weekday() >= 5
    assert len(asked) > 1, f"only asked {asked}"
    assert all(date.fromisoformat(d).weekday() < 5 for d in asked)


@pytest.mark.asyncio
async def test_days_already_archived_are_not_refetched(
    patch_session, db_session: AsyncSession,
):
    """Steady state is one call for today, not a full re-walk."""
    from tasks import ingest_govt_bank_flow_tw

    for day in _recent_weekdays(5)[:-1]:
        db_session.add(TwGovtBankFlowDaily(
            market="TW", ts=day, bank_name="臺灣銀行",
            buy_amount=1, sell_amount=1, source="finmind",
        ))
    await db_session.commit()
    archived = {d.isoformat() for d in _recent_weekdays(5)[:-1]}

    asked: list[str] = []

    async def _fetch(day_iso: str) -> list[dict]:
        asked.append(day_iso)
        return []

    with patch("tasks.ingest_govt_bank_flow_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_govt_bank_flow_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_govt_bank_flow_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.finmind.get_government_bank_flow_market_wide",
               _fetch):
        await ingest_govt_bank_flow_tw.run()

    assert not (archived & set(asked)), f"refetched {archived & set(asked)}"


@pytest.mark.asyncio
async def test_sub_aggregate_rows_per_bank_day_get_summed(
    patch_session, db_session: AsyncSession,
):
    """Regression: FinMind splits each bank-day into many sub-aggregate
    rows (one per underlying stock / sector). Without summing, the
    upsert hits postgres' `ON CONFLICT DO UPDATE command cannot affect
    row a second time` cardinality check. We sum the sub-rows into the
    daily total per bank before insert."""
    from tasks import ingest_govt_bank_flow_tw

    # Three sub-rows for (day, 兆豐), one row for (day, 第一).
    day = _recent_weekdays(1)[0].isoformat()
    payload = [
        _row(day, "兆豐", 100_000_000, 80_000_000),
        _row(day, "兆豐",  50_000_000, 40_000_000),
        _row(day, "兆豐",  20_000_000, 10_000_000),
        _row(day, "第一",  30_000_000, 25_000_000),
    ]
    with patch("tasks.ingest_govt_bank_flow_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_govt_bank_flow_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_govt_bank_flow_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.finmind.get_government_bank_flow_market_wide",
               _market_wide_stub(payload)):
        await ingest_govt_bank_flow_tw.run()

    rows = (await db_session.scalars(select(TwGovtBankFlowDaily))).all()
    assert len(rows) == 2
    by_bank = {r.bank_name: r for r in rows}
    assert int(by_bank["兆豐"].buy_amount) == 170_000_000
    assert int(by_bank["兆豐"].sell_amount) == 130_000_000
    assert int(by_bank["第一"].buy_amount) == 30_000_000


@pytest.mark.asyncio
async def test_paywall_response_marked_skipped(patch_session):
    """If FinMind moves this dataset to paid-only later (it's
    already sponsor-tier as of 2026-04, but they could re-tier),
    the shared detector triggers fail-soft."""
    from tasks import ingest_govt_bank_flow_tw

    body_msg = "Your level is register. Please update your user level."
    record_failure_mock = AsyncMock()
    clear_failures_mock = AsyncMock()
    health_mock = AsyncMock()
    with patch("tasks.ingest_govt_bank_flow_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_govt_bank_flow_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_govt_bank_flow_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_govt_bank_flow_tw.record_failure", record_failure_mock), \
         patch("tasks.ingest_govt_bank_flow_tw.clear_failures", clear_failures_mock), \
         patch("tasks.ingest_govt_bank_flow_tw.record_health", health_mock), \
         patch("tasks.ingest_govt_bank_flow_tw.finmind.get_government_bank_flow_market_wide",
               AsyncMock(side_effect=_http_status_error(400, body_msg))):
        await ingest_govt_bank_flow_tw.run()

    record_failure_mock.assert_not_called()
    clear_failures_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped" in kwargs["error"].lower()


@pytest.mark.asyncio
async def test_read_recent_govt_bank_flow_aggregates_across_banks(
    db_session: AsyncSession,
):
    """Read tier sums per-bank rows into one daily aggregate.
    Discussion context only cares about the headline net flow."""
    from services.ingest.repository import (
        GovtBankFlowRow, read_recent_govt_bank_flow, upsert_govt_bank_flows,
    )
    today = date.today()
    await upsert_govt_bank_flows(db_session, [
        GovtBankFlowRow("TW", today, "臺灣銀行",
                        5_000_000_000, 3_000_000_000, "finmind"),
        GovtBankFlowRow("TW", today, "兆豐國際",
                        2_000_000_000, 1_500_000_000, "finmind"),
        GovtBankFlowRow("TW", today, "華南銀行",
                        1_000_000_000, 500_000_000, "finmind"),
    ])
    out = await read_recent_govt_bank_flow(db_session, market="TW", days=1)
    assert len(out) == 1
    assert out[0]["date"] == today.isoformat()
    assert out[0]["buy_total"] == 8_000_000_000
    assert out[0]["sell_total"] == 5_000_000_000
    assert out[0]["net"] == 3_000_000_000
