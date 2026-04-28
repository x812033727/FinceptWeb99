"""Unit tests for news sentiment scoring.

Covers:
  - Bucket thresholds (bullish / bearish / neutral cutoffs)
  - JSON parsing — clean array, code-fenced, malformed
  - score_pending stamps `sentiment_scored_at` for every input row
    (so the next pass doesn't keep retrying the same un-scoreable
    headlines), but only fills score/label for ones the LLM returned
  - Aggregator computes correct counts + average
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news_article import NewsArticle
from services import news_sentiment_service
from services.ingest.repository import NewsArticleRow, insert_news_articles


# ── helpers ────────────────────────────────────────────────────────


def _row(
    title: str,
    link: str,
    *,
    symbol: str | None = "2330",
    published_at: datetime | None = None,
) -> NewsArticleRow:
    return NewsArticleRow(
        market="TW",
        symbol=symbol,
        published_at=published_at or datetime.now(UTC),
        title=title,
        link=link,
        publisher="鉅亨網",
        summary=None,
        payload=None,
        source="finmind",
    )


def _stream_text(text: str):
    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": text}
    return _gen


# ── bucket ─────────────────────────────────────────────────────────


def test_bucket_thresholds():
    assert news_sentiment_service._bucket(0.5) == "bullish"
    assert news_sentiment_service._bucket(0.25) == "bullish"
    assert news_sentiment_service._bucket(0.24) == "neutral"
    assert news_sentiment_service._bucket(0.0) == "neutral"
    assert news_sentiment_service._bucket(-0.24) == "neutral"
    assert news_sentiment_service._bucket(-0.25) == "bearish"
    assert news_sentiment_service._bucket(-0.9) == "bearish"


# ── parse ──────────────────────────────────────────────────────────


def test_parse_response_clean_array():
    raw = '[{"id": 1, "score": 0.8, "reason": "強勁營收"}]'
    parsed = news_sentiment_service._parse_response(raw)
    assert parsed == [{"id": 1, "score": 0.8, "reason": "強勁營收"}]


def test_parse_response_strips_code_fence():
    raw = '```json\n[{"id": 1, "score": -0.5}]\n```'
    parsed = news_sentiment_service._parse_response(raw)
    assert parsed == [{"id": 1, "score": -0.5}]


def test_parse_response_malformed_returns_empty():
    parsed = news_sentiment_service._parse_response("not json at all")
    assert parsed == []


def test_parse_response_object_instead_of_array_returns_empty():
    parsed = news_sentiment_service._parse_response('{"id": 1, "score": 0.5}')
    assert parsed == []


# ── score_pending end-to-end ───────────────────────────────────────


@pytest.mark.asyncio
async def test_score_pending_writes_back_scores(db_session: AsyncSession):
    await insert_news_articles(db_session, [
        _row("台積電 Q1 EPS 創歷史新高",
             "https://example.com/sent_1", symbol="SENT_1"),
        _row("聯發科法說會釋出保守展望",
             "https://example.com/sent_2", symbol="SENT_2"),
    ])

    inserted = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.symbol.in_(["SENT_1", "SENT_2"]))
        .order_by(NewsArticle.id)
    )).all()
    id1, id2 = inserted[0].id, inserted[1].id

    fake_response = (
        f'[{{"id": {id1}, "score": 0.8, "reason": "EPS 創高"}},'
        f' {{"id": {id2}, "score": -0.4, "reason": "保守展望"}}]'
    )
    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text(fake_response),
    ):
        result = await news_sentiment_service.score_pending(
            db=db_session, batch_size=10, max_batches=1,
        )

    assert result["considered"] >= 2
    assert result["scored"] >= 2

    refreshed = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.id.in_([id1, id2]))
        .order_by(NewsArticle.id)
    )).all()
    a, b = refreshed[0], refreshed[1]
    assert a.sentiment_score == 0.8
    assert a.sentiment_label == "bullish"
    assert a.sentiment_scored_at is not None
    assert b.sentiment_score == -0.4
    assert b.sentiment_label == "bearish"


@pytest.mark.asyncio
async def test_score_pending_stamps_unscored_to_avoid_reprocessing(
    db_session: AsyncSession,
):
    """If the LLM returns nothing for a row, we still stamp
    sentiment_scored_at so the next pass doesn't keep retrying it.
    """
    await insert_news_articles(db_session, [
        _row("空 LLM 回應的標題",
             "https://example.com/sent_unscored", symbol="UNSCORED_1"),
    ])
    inserted = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.symbol == "UNSCORED_1")
    )
    aid = inserted.id

    # Empty array — LLM returned nothing for our row.
    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text("[]"),
    ):
        await news_sentiment_service.score_pending(
            db=db_session, batch_size=10, max_batches=1,
        )

    refreshed = await db_session.get(NewsArticle, aid)
    assert refreshed.sentiment_score is None
    assert refreshed.sentiment_label is None
    assert refreshed.sentiment_scored_at is not None  # but stamped


@pytest.mark.asyncio
async def test_score_pending_skips_when_all_scored(db_session: AsyncSession):
    await insert_news_articles(db_session, [
        _row("已被打過分的舊新聞",
             "https://example.com/sent_done", symbol="DONE_1"),
    ])
    row = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.symbol == "DONE_1")
    )
    row.sentiment_score = 0.5
    row.sentiment_label = "bullish"
    row.sentiment_scored_at = datetime.now(UTC)
    await db_session.commit()

    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text("[]"),
    ) as mocked:
        result = await news_sentiment_service.score_pending(
            db=db_session, batch_size=10, max_batches=2,
        )

    assert result["considered"] == 0
    assert result["scored"] == 0
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_read_recent_market_sentiment_aggregates(db_session: AsyncSession):
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("a", "https://example.com/agg_a", symbol=None,
             published_at=now - timedelta(hours=1)),
        _row("b", "https://example.com/agg_b", symbol=None,
             published_at=now - timedelta(hours=2)),
        _row("c", "https://example.com/agg_c", symbol=None,
             published_at=now - timedelta(hours=3)),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like("https://example.com/agg_%"))
    )).all()
    rows[0].sentiment_score = 0.6
    rows[0].sentiment_label = "bullish"
    rows[0].sentiment_scored_at = now
    rows[1].sentiment_score = -0.4
    rows[1].sentiment_label = "bearish"
    rows[1].sentiment_scored_at = now
    rows[2].sentiment_score = 0.1
    rows[2].sentiment_label = "neutral"
    rows[2].sentiment_scored_at = now
    await db_session.commit()

    out = await news_sentiment_service.read_recent_market_sentiment(
        db_session, market="TW", limit=10, max_age_hours=24,
    )
    assert out["bullish"] == 1
    assert out["bearish"] == 1
    assert out["neutral"] == 1
    assert round(out["avg_score"], 2) == 0.10
    assert len(out["headlines"]) == 3
