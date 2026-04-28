"""Tests for tasks.ingest_news_tw — the hourly TW news job."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news_article import NewsArticle


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.ingest_news_tw.AsyncSessionLocal", return_value=_CM()):
        yield


def _finmind_item(idx: int, *, symbol: str | None = None) -> dict:
    return {
        "title": f"Headline IT{idx}",
        "link": f"https://example.com/news/it{idx}",
        "source_name": "鉅亨網",
        "description": "summary text",
        "published_at": "2026-04-28 10:00:00",   # Asia/Taipei naive
        "symbol": symbol,
    }


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=False)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock()) as fm:
        await ingest_news_tw.run()

    fm.assert_not_awaited()


@pytest.mark.asyncio
async def test_finmind_failure_records_unhealthy(patch_session):
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.finmind.get_news",
               AsyncMock(side_effect=RuntimeError("finmind 503"))), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "finmind_unavailable" in kwargs["error"]


@pytest.mark.asyncio
async def test_empty_result_records_ok_zero(patch_session):
    """Quota-exhausted FinMind returns []. The run should mark itself
    healthy with row_count=0 — the cron successfully executed."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock(return_value=[])), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_success_inserts_rows(patch_session, db_session: AsyncSession):
    from tasks import ingest_news_tw

    items = [
        _finmind_item(101, symbol=None),       # market-wide
        _finmind_item(102, symbol="2330"),      # per-symbol
    ]

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock(return_value=items)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    rows = (await db_session.scalars(
        select(NewsArticle).where(
            NewsArticle.link.in_([
                "https://example.com/news/it101",
                "https://example.com/news/it102",
            ])
        )
    )).all()
    assert len(rows) == 2

    by_link = {r.link: r for r in rows}
    market_wide = by_link["https://example.com/news/it101"]
    assert market_wide.symbol is None

    per_sym = by_link["https://example.com/news/it102"]
    assert per_sym.symbol == "2330"

    # FinMind times are Asia/Taipei naive — 10:00 CST = 02:00 UTC. SQLite
    # drops tzinfo on read so we compare on the underlying wall-clock UTC.
    saved = per_sym.published_at
    if saved.tzinfo is not None:
        saved = saved.astimezone(UTC).replace(tzinfo=None)
    assert saved == datetime(2026, 4, 28, 2, 0)

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_rerun_dedupes(patch_session, db_session: AsyncSession):
    """Running twice with the same payload must not duplicate rows."""
    from tasks import ingest_news_tw

    items = [_finmind_item(201, symbol="2330")]

    common = (
        patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)),
        patch("tasks.ingest_news_tw.release_lock", AsyncMock()),
        patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock(return_value=items)),
        patch("tasks.ingest_news_tw.record_health", AsyncMock()),
    )
    for ctx in common:
        ctx.__enter__()
    try:
        await ingest_news_tw.run()
        await ingest_news_tw.run()
    finally:
        for ctx in common:
            ctx.__exit__(None, None, None)

    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link == "https://example.com/news/it201")
    )).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_blank_published_at_falls_back_to_now(patch_session, db_session: AsyncSession):
    """Connector rows missing `published_at` must not be dropped — we
    fall back to current time so the article is at least retrievable."""
    from tasks import ingest_news_tw

    items = [{
        "title": "No date headline",
        "link": "https://example.com/news/nodate",
        "source_name": "鉅亨網",
        "description": "",
        "published_at": "",
        "symbol": "2330",
    }]

    before = datetime.now(UTC)
    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock(return_value=items)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()):
        await ingest_news_tw.run()
    after = datetime.now(UTC)

    row = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.link == "https://example.com/news/nodate")
    )
    assert row is not None
    saved = row.published_at
    # SQLite strips tzinfo on read — normalize so we can compare against
    # the UTC `before` / `after` bookends.
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= saved <= after + timedelta(seconds=1)
