"""Repository tests for the full-text enrichment queue helpers
(`read_news_needing_body` / `update_news_body`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services.ingest.repository import (
    NewsArticleRow,
    insert_news_articles,
    read_news_needing_body,
    update_news_body,
)


def _row(source: str, title: str, *, age_days: int = 0) -> NewsArticleRow:
    return NewsArticleRow(
        market="TW",
        symbol=None,
        published_at=datetime.now(UTC) - timedelta(days=age_days),
        title=title,
        link=f"https://pub.example.com/{title}",
        publisher=None,
        summary=None,
        payload=None,
        source=source,
    )


@pytest.mark.asyncio
async def test_needing_body_scopes_to_sources_and_null_body(
    db_session: AsyncSession,
):
    await insert_news_articles(db_session, [
        _row("cnyes", "a"),
        _row("cnyes", "b"),
        _row("google_news_tw", "c"),   # not a direct feed → excluded
    ])
    got = await read_news_needing_body(
        db_session, source_keys=["cnyes"], limit=10,
    )
    titles = {link.rsplit("/", 1)[-1] for _, link in got}
    assert titles == {"a", "b"}


@pytest.mark.asyncio
async def test_needing_body_excludes_old_articles(db_session: AsyncSession):
    await insert_news_articles(db_session, [
        _row("cnyes", "fresh", age_days=1),
        _row("cnyes", "stale", age_days=30),
    ])
    got = await read_news_needing_body(
        db_session, source_keys=["cnyes"], limit=10, max_age_days=7,
    )
    assert {link.rsplit("/", 1)[-1] for _, link in got} == {"fresh"}


@pytest.mark.asyncio
async def test_update_body_stamps_and_removes_from_queue(
    db_session: AsyncSession,
):
    await insert_news_articles(db_session, [_row("cnyes", "a")])
    queue = await read_news_needing_body(
        db_session, source_keys=["cnyes"], limit=10,
    )
    assert len(queue) == 1
    article_id, _ = queue[0]

    await update_news_body(db_session, article_id, "extracted body text")
    # Now attempted → no longer in the queue.
    assert await read_news_needing_body(
        db_session, source_keys=["cnyes"], limit=10,
    ) == []


@pytest.mark.asyncio
async def test_update_body_none_still_stamps(db_session: AsyncSession):
    """A failed extract (body=None) still marks the attempt so the
    enricher won't retry a dead page forever."""
    await insert_news_articles(db_session, [_row("cnyes", "a")])
    queue = await read_news_needing_body(
        db_session, source_keys=["cnyes"], limit=10,
    )
    article_id, _ = queue[0]
    await update_news_body(db_session, article_id, None)
    assert await read_news_needing_body(
        db_session, source_keys=["cnyes"], limit=10,
    ) == []
