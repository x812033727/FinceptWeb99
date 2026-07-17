"""Tests for `tasks.ingest_taiex_tr_history` — daily TAIEX TR
(含息報酬指數) ingest. Mirrors `test_ingest_taiex_history_task.py`'s
shape but pulls from FinMind sponsor instead of TWSE.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_taiex_tr_history.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _bar(time_str: str, close: float) -> dict:
    return {
        "time": time_str,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 0,
    }


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
async def test_writes_taiex_tr_rows_under_synthetic_symbol(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_taiex_tr_history

    payload = [
        _bar("2026-04-28", 35_000.50),
        _bar("2026-04-29", 35_125.00),
        _bar("2026-04-30", 35_080.20),
    ]
    with patch("tasks.ingest_taiex_tr_history.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_taiex_tr_history.release_lock", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_taiex_tr_history.clear_failures", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.record_health", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.finmind.get_taiex_total_return_index",
               AsyncMock(return_value=payload)):
        await ingest_taiex_tr_history.run()

    rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "_TAIEX_TR")
    )).all()
    assert len(rows) == 3
    by_date = {r.ts: r for r in rows}
    assert float(by_date[date(2026, 4, 29)].close) == 35_125.00
    # All four OHL fields collapsed to close (index has no real OHL).
    bar = by_date[date(2026, 4, 30)]
    assert float(bar.open) == 35_080.20
    assert float(bar.high) == 35_080.20
    assert float(bar.low) == 35_080.20


@pytest.mark.asyncio
async def test_skips_rows_with_zero_or_missing_close(
    patch_session, db_session: AsyncSession,
):
    """Defensive: a single bad row doesn't poison the whole batch.
    A non-positive close means upstream is reporting bad data — drop."""
    from tasks import ingest_taiex_tr_history

    payload = [
        _bar("2026-04-29", 35_000.0),
        _bar("2026-04-30", 0.0),     # bad — drop
        {"time": "2026-04-28"},      # missing close — drop
        _bar("bad-date", 35_100),    # unparseable date — drop
    ]
    with patch("tasks.ingest_taiex_tr_history.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_taiex_tr_history.release_lock", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_taiex_tr_history.clear_failures", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.record_health", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.finmind.get_taiex_total_return_index",
               AsyncMock(return_value=payload)):
        await ingest_taiex_tr_history.run()

    rows = (await db_session.scalars(
        select(OhlcvDaily).where(OhlcvDaily.symbol == "_TAIEX_TR")
    )).all()
    assert {r.ts for r in rows} == {date(2026, 4, 29)}


@pytest.mark.asyncio
async def test_paywall_response_marked_skipped(patch_session):
    """Sponsor-tier dataset; if FinMind re-tiers later we want the
    same fail-soft as PR #183 / #189 / #190."""
    from tasks import ingest_taiex_tr_history

    body_msg = "Your level is register. Please update your user level."
    record_failure_mock = AsyncMock()
    clear_failures_mock = AsyncMock()
    health_mock = AsyncMock()
    with patch("tasks.ingest_taiex_tr_history.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_taiex_tr_history.release_lock", AsyncMock()), \
         patch("tasks.ingest_taiex_tr_history.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_taiex_tr_history.record_failure", record_failure_mock), \
         patch("tasks.ingest_taiex_tr_history.clear_failures", clear_failures_mock), \
         patch("tasks.ingest_taiex_tr_history.record_health", health_mock), \
         patch("tasks.ingest_taiex_tr_history.finmind.get_taiex_total_return_index",
               AsyncMock(side_effect=_http_status_error(400, body_msg))):
        await ingest_taiex_tr_history.run()

    record_failure_mock.assert_not_called()
    clear_failures_mock.assert_awaited_once()
    kwargs = health_mock.await_args.kwargs
    assert "skipped" in kwargs["error"].lower()


@pytest.mark.asyncio
async def test_get_history_short_circuits_for_synthetic_symbols(
    db_session: AsyncSession,
):
    """`tw_market_service.get_history` must NOT fall through to the
    upstream waterfall for synthetic-prefix symbols (`_TAIEX`,
    `_TAIEX_TR`). TWSE / FinMind per-symbol APIs don't accept index
    codes; falling through would 404 and cache an empty result."""
    from services import tw_market_service as svc

    # Seed an `_TAIEX_TR` archive row so the short-circuit path has
    # something to return.
    from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars
    await upsert_ohlcv_bars(db_session, [
        OhlcvBar(market="TW", symbol="_TAIEX_TR",
                 ts=date.fromordinal(date.today().toordinal() - 2),
                 open=35000, high=35000, low=35000, close=35000,
                 volume=0, source="finmind"),
    ])

    twse_mock = AsyncMock(return_value=[])
    finmind_mock = AsyncMock(return_value=[])
    with patch.object(svc, "cache_get_json", AsyncMock(return_value=None)), \
         patch.object(svc, "cache_set_json", AsyncMock()), \
         patch.object(svc.twse, "get_daily_ohlcv", twse_mock), \
         patch.object(svc.finmind, "get_daily_ohlcv", finmind_mock):
        bars = await svc.get_history("_TAIEX_TR", months=1)

    assert len(bars) == 1
    assert bars[0]["close"] == 35000
    # Critical: NEITHER upstream was called.
    twse_mock.assert_not_called()
    finmind_mock.assert_not_called()


@pytest.mark.asyncio
async def test_tr_connector_queries_taiex_data_id():
    """Regression: data_id must be "TAIEX" — "IR0001" returns
    success + zero rows on this dataset, which kept _TAIEX_TR empty
    since the job shipped."""
    import data.tw.finmind_connector as fm

    q = AsyncMock(return_value=[{"date": "2026-07-01", "price": 107781.17}])
    with patch.object(fm, "_query", q):
        rows = await fm.get_taiex_total_return_index("2026-07-01")
    assert q.await_args.args[0] == "TaiwanStockTotalReturnIndex"
    assert q.await_args.args[1] == "TAIEX"
    assert rows[0]["close"] == 107781.17
