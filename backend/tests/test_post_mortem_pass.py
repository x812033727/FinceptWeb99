from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from models.ohlcv_daily import OhlcvDaily
from services import post_mortem_service


@pytest.mark.asyncio
async def test_no_conclusion_is_a_silent_noop():
    db = AsyncMock()
    disc = SimpleNamespace(id=uuid4(), conclusion=None, market="TW")
    db.refresh = AsyncMock()
    with patch.object(post_mortem_service, "build_post_mortem_message", new=AsyncMock()) as build:
        await post_mortem_service.run_post_mortem_pass(db, disc, uuid4())
    build.assert_not_awaited()


def _bar(symbol: str, ts: date, close: float) -> OhlcvDaily:
    return OhlcvDaily(
        market="TW", symbol=symbol, ts=ts,
        open=close, high=close, low=close, close=close,
        volume=1_000_000, source="test",
    )


@pytest.mark.asyncio
async def test_run_post_mortem_pass_completes_for_live_win_discussion(db_session):
    """C1 regression, end-to-end: `run_post_mortem_pass` is the shared
    entry point for both the backtest sweep and the live verify path
    (`maybe_run_live_post_mortem`). Live discussions always have
    `as_of_date is None`; before the fix, `build_post_mortem_message`
    hard-raised on that, and the fail-closed `except` in
    `maybe_run_live_post_mortem` silently swallowed it — live
    discussions never actually got critiqued. This drives a real
    `as_of_date=None` discussion through the real
    `build_post_mortem_message` (only the `discussion_service` calls
    are mocked) and asserts the win-lesson extraction path actually
    ran instead of the whole pass silently no-op'ing on an exception.
    """
    db_session.add_all([
        _bar("2330", date(2026, 3, 23), 100.0),
        _bar("2330", date(2026, 3, 24), 102.0),
        _bar("2330", date(2026, 3, 25), 108.0),   # +8 % vs entry → clear win
        _bar("2330", date(2026, 3, 26), 107.0),
        _bar("2330", date(2026, 3, 27), 106.0),
        _bar("2330", date(2026, 3, 30), 107.0),
    ])
    await db_session.commit()

    # AsyncSession.refresh() requires an ORM-identity-mapped instance;
    # this discussion is a plain duck-typed stand-in, so stub refresh
    # to a no-op while keeping the rest of the real session (and its
    # ohlcv_daily rows above) intact for build_post_mortem_message.
    db_session.refresh = AsyncMock()

    disc = SimpleNamespace(
        id=uuid4(), owner_id=uuid4(), market="TW",
        conclusion={"recommended_symbols": ["2330"]},
        as_of_date=None,
        created_at=datetime(2026, 3, 23, 6, 0, tzinfo=UTC),
    )

    with patch(
        "services.discussion_service.extract_winning_thesis_lessons",
        new=AsyncMock(),
    ) as extract:
        await post_mortem_service.run_post_mortem_pass(
            db_session, disc, disc.owner_id,
        )

    extract.assert_awaited_once()
