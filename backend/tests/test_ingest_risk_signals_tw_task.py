"""Tests for `tasks.ingest_risk_signals_tw` — combined cron for the
disposition / suspended / day-trading risk-warning datasets.

Pinned scenarios:
  - happy path runs all three sub-steps + writes rows
  - one-substep paywall keeps the other two going (graceful degrade)
  - one-substep transient outage triggers `auto-backoff armed` health
  - read-tier helpers compute the right aggregates (active filter,
    day-trading ratio threshold)
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_risk_signals import (
    TwStockDayTradingDaily,
    TwStockDisposition,
    TwStockSuspended,
)


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_risk_signals_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _http_status_error(status_code: int, body_msg: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    response = httpx.Response(
        status_code, request=request,
        json={"msg": body_msg, "status": status_code},
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


# ── happy path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_three_substeps_succeed_writes_rows(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_risk_signals_tw

    today = date.today()
    health_mock = AsyncMock()
    with patch("tasks.ingest_risk_signals_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_risk_signals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_risk_signals_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_risk_signals_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_risk_signals_tw.record_health", health_mock), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_disposition_market_wide",
               AsyncMock(return_value=[
                   {"symbol": "9999", "period_start": today.isoformat(),
                    "period_end": (today + timedelta(days=10)).isoformat(),
                    "level": 1, "classification": "處置",
                    "reason": "5 日漲幅異常"}
               ])), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_suspended_market_wide",
               AsyncMock(return_value=[
                   {"symbol": "8888", "date": today.isoformat(),
                    "status": "suspended", "reason": "重大訊息待澄清"}
               ])), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_day_trading_market_wide",
               AsyncMock(return_value=[
                   {"symbol": "2330", "date": today.isoformat(),
                    "volume": "10000", "buy_amount": "3500", "sell_amount": "3500"}
               ])):
        await ingest_risk_signals_tw.run()

    disp = (await db_session.scalars(select(TwStockDisposition))).all()
    susp = (await db_session.scalars(select(TwStockSuspended))).all()
    dt = (await db_session.scalars(select(TwStockDayTradingDaily))).all()
    assert {r.symbol for r in disp} == {"9999"}
    assert {r.symbol for r in susp} == {"8888"}
    assert {r.symbol for r in dt} == {"2330"}

    health_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 3


@pytest.mark.asyncio
async def test_paywalled_substep_does_not_block_others(
    patch_session, db_session: AsyncSession,
):
    """If FinMind paywalls one of the three datasets but the other
    two work, we still write the working data and the health row
    flips to yellow `skipped:` rather than red error."""
    from tasks import ingest_risk_signals_tw

    today = date.today()
    paywall_msg = "Your level is register. Please update your user level."
    record_failure_mock = AsyncMock()
    health_mock = AsyncMock()
    with patch("tasks.ingest_risk_signals_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_risk_signals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_risk_signals_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_risk_signals_tw.record_failure", record_failure_mock), \
         patch("tasks.ingest_risk_signals_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_risk_signals_tw.record_health", health_mock), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_disposition_market_wide",
               AsyncMock(side_effect=_http_status_error(400, paywall_msg))), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_suspended_market_wide",
               AsyncMock(return_value=[
                   {"symbol": "8888", "date": today.isoformat(),
                    "status": "suspended"}
               ])), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_day_trading_market_wide",
               AsyncMock(return_value=[])):
        await ingest_risk_signals_tw.run()

    record_failure_mock.assert_not_called()
    susp = (await db_session.scalars(select(TwStockSuspended))).all()
    assert {r.symbol for r in susp} == {"8888"}  # still wrote
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped" in kwargs["error"].lower()
    assert "disposition" in kwargs["error"].lower()
    assert "suspended" in kwargs["error"].lower()  # listed as success


@pytest.mark.asyncio
async def test_genuine_outage_arms_backoff(patch_session):
    from tasks import ingest_risk_signals_tw

    record_failure_mock = AsyncMock(return_value=1)
    health_mock = AsyncMock()
    with patch("tasks.ingest_risk_signals_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_risk_signals_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_risk_signals_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_risk_signals_tw.record_failure", record_failure_mock), \
         patch("tasks.ingest_risk_signals_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_risk_signals_tw.record_health", health_mock), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_disposition_market_wide",
               AsyncMock(side_effect=_http_status_error(503, "FinMind unavailable"))), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_suspended_market_wide",
               AsyncMock(return_value=[])), \
         patch("tasks.ingest_risk_signals_tw.finmind.get_day_trading_market_wide",
               AsyncMock(return_value=[])):
        await ingest_risk_signals_tw.run()

    record_failure_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert "auto-backoff armed" in kwargs["error"]


# ── repository read helpers ──────────────────────────────────────

@pytest.mark.asyncio
async def test_read_active_dispositions_filters_by_period(
    db_session: AsyncSession,
):
    from services.ingest.repository import (
        DispositionRow, read_active_dispositions, upsert_dispositions,
    )
    today = date(2026, 5, 1)
    await upsert_dispositions(db_session, [
        DispositionRow("TW", "9999", date(2026, 4, 15),
                       date(2026, 6, 15), "處置", 1, "5日漲跌異常",
                       "finmind"),
        DispositionRow("TW", "8888", date(2025, 1, 1),
                       date(2025, 3, 1), "處置", 2, "expired",
                       "finmind"),
        # NULL period_end → still active
        DispositionRow("TW", "7777", date(2026, 4, 28),
                       None, "警示", 1, "fresh", "finmind"),
    ])
    rows = await read_active_dispositions(db_session, market="TW", as_of=today)
    assert {r["symbol"] for r in rows} == {"9999", "7777"}


@pytest.mark.asyncio
async def test_read_high_day_trading_ratio(db_session: AsyncSession):
    """Stock with > 60% day-trading ratio shows up; lower ones drop."""
    from services.ingest.repository import (
        DayTradingRow, read_high_day_trading_ratio, upsert_day_trading,
    )
    today = date.today()
    await upsert_day_trading(db_session, [
        # ratio = (3500 + 3500) / 2 / 10000 = 0.35 → drop
        DayTradingRow("TW", "2330", today, 10000, 3500, 3500, "finmind"),
        # ratio = (4000 + 4000) / 2 / 10000 = 0.40 → drop
        DayTradingRow("TW", "2454", today, 10000, 4000, 4000, "finmind"),
        # ratio = (7000 + 7000) / 2 / 10000 = 0.70 → keep
        DayTradingRow("TW", "1234", today, 10000, 7000, 7000, "finmind"),
        # ratio = (8500 + 8500) / 2 / 10000 = 0.85 → keep, top
        DayTradingRow("TW", "5678", today, 10000, 8500, 8500, "finmind"),
        # zero volume → drop (defensive)
        DayTradingRow("TW", "0000", today, 0, 5000, 5000, "finmind"),
    ])
    rows = await read_high_day_trading_ratio(
        db_session, market="TW", days=1, threshold=0.6,
    )
    assert [r["symbol"] for r in rows] == ["5678", "1234"]
    assert rows[0]["ratio"] == 0.85
    assert rows[1]["ratio"] == 0.70


@pytest.mark.asyncio
async def test_read_symbol_day_trading_trend_returns_none_when_no_data(
    db_session: AsyncSession,
):
    """No rows in window for the symbol → None (not zero/empty dict).
    Caller folds None into the per-symbol signals as `day_trading_trend=None`."""
    from services.ingest.repository import read_symbol_day_trading_trend
    out = await read_symbol_day_trading_trend(
        db_session, market="TW", symbol="DOES_NOT_EXIST",
        days=5, as_of=date(2026, 4, 30),
    )
    assert out is None


@pytest.mark.asyncio
async def test_read_symbol_day_trading_trend_classifies_rising(
    db_session: AsyncSession,
):
    """5 sessions where day-trading ratio walks 0.30 → 0.70 → trend
    must classify as 'rising'."""
    from services.ingest.repository import (
        DayTradingRow, read_symbol_day_trading_trend, upsert_day_trading,
    )
    base = date(2026, 4, 1)
    # ratios: day1=0.30, day2=0.40, day3=0.50, day4=0.60, day5=0.70
    rows = []
    for i, side_per_share in enumerate([3000, 4000, 5000, 6000, 7000]):
        rows.append(DayTradingRow(
            "TW", "2330", base + timedelta(days=i),
            10000, side_per_share, side_per_share, "finmind",
        ))
    await upsert_day_trading(db_session, rows)

    out = await read_symbol_day_trading_trend(
        db_session, market="TW", symbol="2330",
        days=5, as_of=base + timedelta(days=4),
    )
    assert out is not None
    assert out["latest_ratio"] == 0.7
    assert out["session_count"] == 5
    assert out["mean_ratio"] == 0.5
    assert out["trend"] == "rising"


@pytest.mark.asyncio
async def test_read_symbol_day_trading_trend_classifies_falling(
    db_session: AsyncSession,
):
    """0.70 → 0.30 → falling."""
    from services.ingest.repository import (
        DayTradingRow, read_symbol_day_trading_trend, upsert_day_trading,
    )
    base = date(2026, 4, 1)
    rows = [
        DayTradingRow(
            "TW", "2330", base + timedelta(days=i),
            10000, side, side, "finmind",
        )
        for i, side in enumerate([7000, 6000, 5000, 4000, 3000])
    ]
    await upsert_day_trading(db_session, rows)

    out = await read_symbol_day_trading_trend(
        db_session, market="TW", symbol="2330",
        days=5, as_of=base + timedelta(days=4),
    )
    assert out is not None
    assert out["trend"] == "falling"
    assert out["latest_ratio"] == 0.3


@pytest.mark.asyncio
async def test_read_symbol_day_trading_trend_classifies_stable(
    db_session: AsyncSession,
):
    """Flat 0.50 across 5 days → stable (within ±10% band of mean)."""
    from services.ingest.repository import (
        DayTradingRow, read_symbol_day_trading_trend, upsert_day_trading,
    )
    base = date(2026, 4, 1)
    rows = [
        DayTradingRow(
            "TW", "2330", base + timedelta(days=i),
            10000, 5000, 5000, "finmind",
        )
        for i in range(5)
    ]
    await upsert_day_trading(db_session, rows)

    out = await read_symbol_day_trading_trend(
        db_session, market="TW", symbol="2330",
        days=5, as_of=base + timedelta(days=4),
    )
    assert out is not None
    assert out["trend"] == "stable"
    assert out["latest_ratio"] == out["mean_ratio"] == 0.5


@pytest.mark.asyncio
async def test_read_symbol_day_trading_trend_skips_zero_volume_sessions(
    db_session: AsyncSession,
):
    """Session with 0 volume can't compute a ratio — must be excluded
    from the mean rather than poisoning it with a /0 fallback."""
    from services.ingest.repository import (
        DayTradingRow, read_symbol_day_trading_trend, upsert_day_trading,
    )
    base = date(2026, 4, 1)
    rows = [
        DayTradingRow("TW", "2330", base, 10000, 5000, 5000, "finmind"),
        DayTradingRow("TW", "2330", base + timedelta(days=1), 0, 0, 0, "finmind"),
        DayTradingRow("TW", "2330", base + timedelta(days=2), 10000, 5000, 5000, "finmind"),
    ]
    await upsert_day_trading(db_session, rows)

    out = await read_symbol_day_trading_trend(
        db_session, market="TW", symbol="2330",
        days=5, as_of=base + timedelta(days=2),
    )
    assert out is not None
    assert out["session_count"] == 2   # zero-vol day excluded
    assert out["mean_ratio"] == 0.5


@pytest.mark.asyncio
async def test_read_recent_suspensions(db_session: AsyncSession):
    from services.ingest.repository import (
        SuspendedRow, read_recent_suspensions, upsert_suspensions,
    )
    today = date.today()
    await upsert_suspensions(db_session, [
        SuspendedRow("TW", "9999", today,
                     "suspended", "重大訊息", "finmind"),
        SuspendedRow("TW", "8888", today - timedelta(days=30),
                     "suspended", "old event", "finmind"),
    ])
    rows = await read_recent_suspensions(db_session, market="TW", days=7)
    assert {r["symbol"] for r in rows} == {"9999"}
