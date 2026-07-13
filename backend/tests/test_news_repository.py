"""Round-trip tests for the NewsArticle repository.

Each test uses a unique link prefix so the dedup_hash differs from
prior tests' rows. Symbols are also unique to keep `read_recent_news`
result sets isolated.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news_article import NewsArticle
from services.ingest.repository import (
    NewsArticleRow,
    compute_dedup_hash,
    insert_news_articles,
    read_recent_news,
)


def _row(
    title: str,
    link: str,
    *,
    symbol: str | None = "NEWS_T1",
    published_at: datetime | None = None,
    publisher: str = "鉅亨網",
    source: str = "finmind",
) -> NewsArticleRow:
    return NewsArticleRow(
        market="TW",
        symbol=symbol,
        published_at=published_at or datetime.now(UTC),
        title=title,
        link=link,
        publisher=publisher,
        summary=None,
        payload=None,
        source=source,
    )


# ── dedup hashing ─────────────────────────────────────────────────

def test_dedup_hash_collapses_punctuation_and_case():
    """Same article with cosmetic title differences hashes identically."""
    a = compute_dedup_hash("台積電 Q1 EPS 創高!", "https://example.com/a")
    b = compute_dedup_hash("台積電 q1 eps 創高", "https://example.com/a")
    assert a == b


def test_dedup_hash_strips_utm_tracking_params():
    a = compute_dedup_hash("Same title", "https://example.com/a?utm_source=fb")
    b = compute_dedup_hash("Same title", "https://example.com/a?ref=foo")
    c = compute_dedup_hash("Same title", "https://example.com/a")
    assert a == b == c


def test_dedup_hash_keeps_meaningful_query_params():
    """Non-tracking query params still affect the hash so different
    article IDs don't collide."""
    a = compute_dedup_hash("title", "https://example.com/a?id=1")
    b = compute_dedup_hash("title", "https://example.com/a?id=2")
    assert a != b


# ── insert / dedup ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_persists_row(db_session: AsyncSession):
    rows = [_row("Headline 1", "https://example.com/news/n1", symbol="NEWS_T1")]
    written = await insert_news_articles(db_session, rows)
    assert written == 1

    saved = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.title == "Headline 1")
    )
    assert saved is not None
    assert saved.symbol == "NEWS_T1"
    assert saved.source == "finmind"


@pytest.mark.asyncio
async def test_duplicate_insert_collapses(db_session: AsyncSession):
    """Same dedup_hash → second insert is a no-op."""
    await insert_news_articles(db_session, [
        _row("Dedup test article", "https://example.com/news/dedup1"),
    ])
    await insert_news_articles(db_session, [
        # Cosmetic differences hash identically.
        _row("Dedup test article!", "https://example.com/news/dedup1?utm_source=x"),
    ])

    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.title.like("Dedup test article%"))
    )).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_insert_skips_rows_missing_title_or_link(db_session: AsyncSession):
    """Connector occasionally returns blank rows — those must not crash
    the insert or pollute the table."""
    rows = [
        _row("Valid headline NEWS_T2", "https://example.com/news/valid_t2", symbol="NEWS_T2"),
        _row("", "https://example.com/news/blanktitle", symbol="NEWS_T2"),
        _row("Has title but no link", "", symbol="NEWS_T2"),
    ]
    written = await insert_news_articles(db_session, rows)
    assert written == 1

    saved = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.symbol == "NEWS_T2")
    )).all()
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_insert_chunks_large_batches_to_avoid_param_limit(
    db_session: AsyncSession, monkeypatch,
):
    """PostgreSQL caps a single statement at 32767 query parameters.
    NewsArticleRow has 10 fields → 3276 rows max in one INSERT.
    A FinMind day-by-day backfill of a busy news symbol over a 31-day
    window can return 5 000+ articles, which used to crash the whole
    batch with `asyncpg.InterfaceError: the number of query arguments
    cannot exceed 32767`. The repo now chunks into 2 000-row batches.

    We verify behaviour by shrinking the chunk size for the test and
    inserting more rows than fit in one chunk — every row must land,
    and asserting `db.execute` was called multiple times."""
    from unittest.mock import AsyncMock

    # `_INSERT_NEWS_CHUNK_ROWS` moved to the `repo.news` submodule in the
    # R3-A1/G8 split; patch it where `insert_news_articles` now reads it.
    from services.ingest.repo import news as repo

    monkeypatch.setattr(repo, "_INSERT_NEWS_CHUNK_ROWS", 3)
    rows = [
        _row(f"chunk row {i}", f"https://example.com/news/chunk_{i}",
             symbol=f"CHUNK_{i}")
        for i in range(7)   # 3 + 3 + 1 → expect 3 executes
    ]

    real_execute = db_session.execute
    spy = AsyncMock(side_effect=real_execute)
    monkeypatch.setattr(db_session, "execute", spy)

    written = await insert_news_articles(db_session, rows)
    assert written == 7
    # 3 chunks of 3, 3, 1 → 3 INSERT statements.
    assert spy.await_count == 3

    saved = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like(
            "https://example.com/news/chunk_%",
        ))
    )).all()
    assert len(saved) == 7


# ── read_recent_news ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_recent_news_orders_newest_first(db_session: AsyncSession):
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("Older", "https://example.com/news/old3",
             symbol="NEWS_T3", published_at=now - timedelta(hours=4)),
        _row("Newer", "https://example.com/news/new3",
             symbol="NEWS_T3", published_at=now - timedelta(hours=1)),
        _row("Newest", "https://example.com/news/newest3",
             symbol="NEWS_T3", published_at=now - timedelta(minutes=10)),
    ])

    out = await read_recent_news(db_session, "TW", symbol="NEWS_T3", limit=5)
    assert [a["title"] for a in out] == ["Newest", "Newer", "Older"]


@pytest.mark.asyncio
async def test_read_recent_news_respects_age_window(db_session: AsyncSession):
    old = datetime.now(UTC) - timedelta(days=60)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    await insert_news_articles(db_session, [
        _row("Old article 4", "https://example.com/news/old4",
             symbol="NEWS_T4", published_at=old),
        _row("Fresh article 4", "https://example.com/news/fresh4",
             symbol="NEWS_T4", published_at=fresh),
    ])

    out = await read_recent_news(db_session, "TW", symbol="NEWS_T4", max_age_days=14)
    assert [a["title"] for a in out] == ["Fresh article 4"]


@pytest.mark.asyncio
async def test_read_recent_news_limit(db_session: AsyncSession):
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row(f"Headline {i}", f"https://example.com/news/limit{i}",
             symbol="NEWS_T5", published_at=now - timedelta(minutes=i))
        for i in range(5)
    ])

    out = await read_recent_news(db_session, "TW", symbol="NEWS_T5", limit=2)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_read_recent_news_market_wide_uses_null_symbol(db_session: AsyncSession):
    """`symbol=None` returns market-wide articles only (NULL symbol)."""
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("Market-wide news 6", "https://example.com/news/mw6",
             symbol=None, published_at=now - timedelta(hours=1)),
        _row("Per-symbol news 6", "https://example.com/news/ps6",
             symbol="NEWS_T6", published_at=now - timedelta(hours=1)),
    ])

    market_wide = await read_recent_news(db_session, "TW", symbol=None, limit=10)
    titles = [a["title"] for a in market_wide]
    assert "Market-wide news 6" in titles
    assert "Per-symbol news 6" not in titles


@pytest.mark.asyncio
async def test_read_recent_news_filters_by_market(db_session: AsyncSession):
    """A US-market article must not satisfy a TW lookup."""
    now = datetime.now(UTC)
    us_row = NewsArticleRow(
        market="US", symbol="NEWS_T7",
        published_at=now,
        title="US news", link="https://example.com/news/us7",
        publisher="WSJ", summary=None, payload=None, source="finnhub",
    )
    await insert_news_articles(db_session, [us_row])

    out = await read_recent_news(db_session, "TW", symbol="NEWS_T7", limit=5)
    assert out == []
