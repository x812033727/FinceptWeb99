"""Tests for services.news_backfill_service.

Coverage:
  - Archive populated → skip backfill (one COUNT, no FinMind call)
  - Archive empty → trigger FinMind, insert rows, return covered=True
  - FinMind returns nothing (paywall / no news) → silent failure,
    covered=False, no exception leaks
  - Concurrent rounds (lock contention) → second caller skips
  - Non-TW market → skip entirely (FinMind dataset is TW-specific)
"""
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services import news_backfill_service
from services.ingest.repository import (
    NewsArticleRow,
    insert_news_articles,
)


def _seed_articles(market: str, n: int, base_dt: datetime) -> list[NewsArticleRow]:
    """Build N distinct news rows clustered around `base_dt` so the
    coverage-window probe sees them."""
    return [
        NewsArticleRow(
            market=market,
            symbol=None,
            published_at=base_dt - timedelta(days=i),
            title=f"seed news {i}",
            link=f"https://example.com/seed/{i}",
            publisher="Test Publisher",
            summary=None,
            payload=None,
            source="test_seed",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_skips_backfill_when_archive_already_covered(
    db_session: AsyncSession,
):
    """Hot path: when there are >= _MIN_ARTICLES_THRESHOLD rows in the
    14-day window, return immediately without calling FinMind."""
    as_of = date(2026, 3, 23)
    base_dt = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)
    await insert_news_articles(
        db_session, _seed_articles("TW", 10, base_dt),
    )

    finmind_mock = AsyncMock()
    with patch.object(
        news_backfill_service, "_do_backfill", new=finmind_mock,
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is True
    assert out["backfilled"] == 0
    finmind_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_triggers_backfill_when_archive_empty(
    db_session: AsyncSession,
):
    """Cold path: with no articles in the window, FinMind backfill
    fires. Verified via mocked `_do_backfill` so the test doesn't need
    real FinMind. Returns `backfilled` count from the helper."""
    as_of = date(2024, 6, 15)  # well before any test seed data

    async def _fake_backfill(*, market, as_of):
        # Simulate FinMind returning rows that get inserted.
        await insert_news_articles(
            db_session,
            _seed_articles("TW", 8, datetime(2024, 6, 10, 9, 0, tzinfo=UTC)),
        )
        return 8

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=_fake_backfill),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["backfilled"] == 8
    assert out["covered"] is True


@pytest.mark.asyncio
async def test_silent_fail_when_finmind_returns_nothing(
    db_session: AsyncSession,
):
    """No paid token / paywalled response / FinMind empty for the
    window: backfill yields zero rows. The helper must return
    `covered=False` without raising — the discussion still runs and
    surfaces the existing empty-archive warning."""
    as_of = date(2024, 6, 15)

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(return_value=0),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["backfilled"] == 0
    assert out["covered"] is False
    assert "error" not in out  # silent — not treated as an error


@pytest.mark.asyncio
async def test_silent_fail_when_finmind_raises(
    db_session: AsyncSession,
):
    """A FinMind 5xx during backfill must not abort the round. Helper
    catches the exception, logs, returns `covered=False, error=...`
    so the caller can record it without re-raising."""
    as_of = date(2024, 6, 15)

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=RuntimeError("FinMind 502")),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is False
    assert "FinMind 502" in out.get("error", "")


@pytest.mark.asyncio
async def test_skips_non_tw_market(db_session: AsyncSession):
    """FinMind's TaiwanStockNews is TW-specific. Calling for a US
    discussion must skip immediately so the helper doesn't waste
    quota on a request that can never return useful data."""
    finmind_mock = AsyncMock()
    with patch.object(
        news_backfill_service, "_do_backfill", new=finmind_mock,
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="US", as_of=date(2024, 6, 15),
        )

    assert out["covered"] is False
    assert out["backfilled"] == 0
    assert out.get("skipped") == "non-tw"
    finmind_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_lock_contention_skips_second_caller(
    db_session: AsyncSession,
):
    """Two rounds for the same date hitting the helper concurrently:
    the Redis lock dedups, second caller bails out with skipped="lock".
    The first caller's backfill eventually populates the archive for
    everyone."""
    as_of = date(2024, 6, 15)

    # First caller: takes the lock (mocked to succeed, but never
    # releases for the duration of test). Second caller should see
    # acquire_lock fail.
    with patch.object(
        news_backfill_service, "acquire_lock",
        new=AsyncMock(return_value=False),
    ), patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(return_value=0),
    ) as backfill_mock:
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is False
    assert out.get("skipped") == "lock"
    backfill_mock.assert_not_awaited()
