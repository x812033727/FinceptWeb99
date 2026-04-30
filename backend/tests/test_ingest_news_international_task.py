"""Tests for tasks.ingest_news_international.

Mirrors the TW news ingest task structure but validates the variant-
specific bits: market='GLOBAL', symbol always None (even when the
upstream connector tags one), source='google_news_intl', and the
international query string is forwarded to the connector.
"""
from datetime import UTC, datetime
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

    with patch(
        "tasks.ingest_news_international.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _gn_item(idx: int, *, symbol: str | None = None) -> dict:
    """Connector-shaped item. We pass `symbol="2026"` to some rows to
    verify the international task strips it (see _to_row docstring)."""
    return {
        "title":        f"FOMC headline IT{idx}",
        "link":         f"https://news.google.com/articles/intl-{idx}",
        "source_name":  "鉅亨網",
        "description":  "summary",
        "published_at": "2026-04-30T08:00:00+00:00",
        "symbol":       symbol,
    }


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_news_international

    with patch(
        "tasks.ingest_news_international.acquire_lock",
        AsyncMock(return_value=False),
    ), patch(
        "tasks.ingest_news_international.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.google_news.get_news", AsyncMock(),
    ) as gn:
        await ingest_news_international.run()

    gn.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_inserts_rows_under_global_market(
    patch_session, db_session: AsyncSession,
):
    """Happy path: rows land in news_articles with market='GLOBAL'."""
    from tasks import ingest_news_international

    items = [_gn_item(1), _gn_item(2)]

    with patch(
        "tasks.ingest_news_international.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_news_international.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_news_international.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.google_news.get_news",
        AsyncMock(return_value=items),
    ) as gn, patch(
        "tasks.ingest_news_international.record_health", AsyncMock(),
    ) as health:
        await ingest_news_international.run()

    # Connector called with the international query, not the TW default.
    kwargs = gn.await_args.kwargs
    assert "FOMC" in kwargs["query"] or "美股" in kwargs["query"]

    rows = (await db_session.scalars(
        select(NewsArticle).where(
            NewsArticle.link.in_([
                "https://news.google.com/articles/intl-1",
                "https://news.google.com/articles/intl-2",
            ])
        )
    )).all()
    assert len(rows) == 2
    for r in rows:
        assert r.market == "GLOBAL"
        assert r.symbol is None
        assert r.source == "google_news_intl"

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_strips_connector_symbol_tags(
    patch_session, db_session: AsyncSession,
):
    """Connector might mis-tag a year ('2026') as a 4-digit stock code.
    The international ingest pipeline must override symbol=None on
    every row so `read_symbol_sentiment(symbol='2026')` doesn't
    surface unrelated international headlines."""
    from tasks import ingest_news_international

    # Connector emits symbol="2026" — the regex matched a year.
    items = [_gn_item(99, symbol="2026")]

    with patch(
        "tasks.ingest_news_international.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_news_international.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_news_international.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.google_news.get_news",
        AsyncMock(return_value=items),
    ), patch(
        "tasks.ingest_news_international.record_health", AsyncMock(),
    ):
        await ingest_news_international.run()

    row = await db_session.scalar(
        select(NewsArticle).where(
            NewsArticle.link == "https://news.google.com/articles/intl-99",
        )
    )
    assert row is not None
    assert row.symbol is None


@pytest.mark.asyncio
async def test_rerun_dedupes(patch_session, db_session: AsyncSession):
    from tasks import ingest_news_international

    items = [_gn_item(301)]

    common = (
        patch(
            "tasks.ingest_news_international.acquire_lock",
            AsyncMock(return_value=True),
        ),
        patch("tasks.ingest_news_international.release_lock", AsyncMock()),
        patch(
            "tasks.ingest_news_international.backoff_remaining_seconds",
            AsyncMock(return_value=0),
        ),
        patch("tasks.ingest_news_international.clear_failures", AsyncMock()),
        patch(
            "tasks.ingest_news_international.google_news.get_news",
            AsyncMock(return_value=items),
        ),
        patch("tasks.ingest_news_international.record_health", AsyncMock()),
    )
    for ctx in common:
        ctx.__enter__()
    try:
        await ingest_news_international.run()
        await ingest_news_international.run()
    finally:
        for ctx in common:
            ctx.__exit__(None, None, None)

    rows = (await db_session.scalars(
        select(NewsArticle).where(
            NewsArticle.link == "https://news.google.com/articles/intl-301",
        )
    )).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_blank_published_at_falls_back_to_now(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_news_international
    from datetime import timedelta

    items = [{
        "title":        "No date headline",
        "link":         "https://news.google.com/articles/intl-nodate",
        "source_name":  "鉅亨網",
        "description":  "",
        "published_at": "",
        "symbol":       None,
    }]

    before = datetime.now(UTC)
    with patch(
        "tasks.ingest_news_international.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_news_international.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_news_international.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_news_international.google_news.get_news",
        AsyncMock(return_value=items),
    ), patch(
        "tasks.ingest_news_international.record_health", AsyncMock(),
    ):
        await ingest_news_international.run()
    after = datetime.now(UTC)

    row = await db_session.scalar(
        select(NewsArticle).where(
            NewsArticle.link == "https://news.google.com/articles/intl-nodate",
        )
    )
    assert row is not None
    saved = row.published_at
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=UTC)
    assert before - timedelta(seconds=1) <= saved <= after + timedelta(seconds=1)
