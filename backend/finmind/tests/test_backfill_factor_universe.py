from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from finmind.models.master import TwDelisting, TwStockInfo
from finmind.models.technical import (
    TwPriceLimitDaily,
    TwStockPriceAdj,
    TwSuspended,
)
from finmind.scripts import backfill
from services import tw_factor_service

_SESSION = object()


@asynccontextmanager
async def _session_context():
    yield _SESSION


@pytest.mark.asyncio
async def test_equity_universe_backfill_discovers_symbols_and_fans_out():
    discover = AsyncMock(return_value=["1101", "2330"])
    run_one = AsyncMock()

    with patch.object(backfill, "FinmindAsyncSessionLocal", _session_context), \
         patch("finmind.scheduler.runner.get_universe_from_tw_stock_info", discover), \
         patch.object(backfill, "_run_one", run_one):
        await backfill._run_equity_universe(
            "TaiwanStockPriceAdj",
            backfill.date(2025, 1, 1),
            backfill.date(2025, 6, 30),
        )

    discover.assert_awaited_once_with(_SESSION, exclude_warrants=True)
    assert run_one.await_count == 2
    assert [call.args[1] for call in run_one.await_args_list] == ["1101", "2330"]


@pytest.mark.asyncio
async def test_factor_sidecar_loader_reads_adjusted_prices_and_lifecycle(finmind_session):
    finmind_session.add_all([
        TwStockPriceAdj(
            market="TWSE", symbol="2330", ts=backfill.date(2025, 6, 30),
            adj_close=1000, adj_factor=1, source="finmind",
        ),
        TwStockInfo(
            market="TWSE", symbol="2330", name_zh="台積電",
            industry_category="半導體業", listed_at=backfill.date(1994, 9, 5),
            is_warrant=False, source="finmind",
        ),
        TwDelisting(
            symbol="1111", delisted_at=backfill.date(2024, 12, 31),
            reason="test", source="twse",
        ),
    ])
    await finmind_session.commit()

    adjusted, stock_info, delistings, ready = (
        await tw_factor_service._load_research_sidecars(
            start=backfill.date(2025, 1, 1), end=backfill.date(2025, 7, 1),
        )
    )

    assert adjusted["2330"]["2025-06-30"] == 1000
    assert stock_info["2330"]["listed_at"] == backfill.date(1994, 9, 5)
    assert delistings["1111"] == backfill.date(2024, 12, 31)
    assert ready is True


@pytest.mark.asyncio
async def test_execution_sidecar_loader_reads_limits_and_suspensions(finmind_session):
    finmind_session.add_all([
        TwPriceLimitDaily(
            market="TWSE", symbol="2330", ts=backfill.date(2025, 6, 30),
            upper_limit=1100, lower_limit=900, source="finmind",
        ),
        TwSuspended(
            symbol="1111", suspended_at=backfill.date(2025, 6, 1),
            resumed_at=backfill.date(2025, 6, 15), reason="test", source="twse",
        ),
    ])
    await finmind_session.commit()

    limits, suspensions, limits_ready, suspensions_ready = (
        await tw_factor_service._load_execution_sidecars(
            start=backfill.date(2025, 6, 1), end=backfill.date(2025, 7, 1),
        )
    )

    assert limits["2330"]["2025-06-30"]["upper_limit"] == 1100
    assert suspensions["1111"] == [
        (backfill.date(2025, 6, 1), backfill.date(2025, 6, 15)),
    ]
    assert limits_ready is True
    assert suspensions_ready is True
