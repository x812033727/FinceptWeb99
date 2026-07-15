from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from scripts import backfill_ohlcv_tw as script


def test_months_back_supports_practical_month_window_and_legacy_years():
    assert script._months_back(date(2025, 7, 15), months=3) == [
        date(2025, 7, 1), date(2025, 6, 1), date(2025, 5, 1),
    ]
    assert len(script._months_back(date(2025, 7, 15), 2)) == 24
    assert script._month_end(date(2024, 2, 1)) == date(2024, 2, 29)


@pytest.mark.asyncio
async def test_resume_ledger_skips_populated_closed_month_but_not_current(
    db_session: AsyncSession,
):
    anchor = date(2025, 5, 1)
    for index in range(15):
        session = anchor + timedelta(days=index)
        db_session.add(OhlcvDaily(
            market="TW", symbol="9199", ts=session, open=10, high=10,
            low=10, close=10, volume=100, source="test",
        ))
    await db_session.flush()

    assert await script._can_resume_month(db_session, "9199", anchor, date(2025, 7, 15)) is True
    assert await script._can_resume_month(
        db_session, "9199", date(2025, 7, 1), date(2025, 7, 15),
    ) is False


@pytest.mark.asyncio
async def test_backfill_symbol_uses_database_resume_without_upstream_call():
    db = AsyncMock()
    with patch.object(script, "_can_resume_month", new_callable=AsyncMock, return_value=True), \
         patch.object(script.twse, "get_daily_ohlcv", new_callable=AsyncMock) as fetch:
        written, skipped = await script._backfill_symbol(
            db, "2330", [date(2025, 5, 1)], False,
            resume=True, today=date(2025, 7, 15),
        )

    assert (written, skipped) == (0, 1)
    fetch.assert_not_awaited()
