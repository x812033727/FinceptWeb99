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
    # Stamp leftover rows' `sentiment_scored_at` so `_fetch_unscored`
    # treats them as already-processed. The shared in-memory SQLite
    # (StaticPool) means prior tests leave behind rows in the table,
    # and without this CI's full-suite run sees ~3 leftovers and the
    # "no LLM calls" assertion below fails.
    #
    # We deliberately leave `sentiment_score` NULL on the leftovers —
    # downstream aggregator tests filter by `sentiment_score IS NOT
    # NULL`, and over-stamping would cause them to over-count.
    from sqlalchemy import update as _update
    now = datetime.now(UTC)
    await db_session.execute(
        _update(NewsArticle)
        .where(NewsArticle.sentiment_scored_at.is_(None))
        .values(sentiment_scored_at=now)
    )

    # DONE_1 itself should look fully scored.
    row = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.symbol == "DONE_1")
    )
    row.sentiment_score = 0.5
    row.sentiment_label = "bullish"
    row.sentiment_scored_at = now
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


# ── per-symbol aggregator ───────────────────────────────────────


@pytest.mark.asyncio
async def test_read_symbol_sentiment_returns_none_when_no_scored_news(
    db_session: AsyncSession,
):
    """A symbol with zero scored articles in the window returns None so
    callers can omit the per-symbol block instead of rendering empty stubs."""
    out = await news_sentiment_service.read_symbol_sentiment(
        db_session, market="TW", symbol="ZZZZ",
    )
    assert out is None


@pytest.mark.asyncio
async def test_read_symbol_sentiment_aggregates_for_one_symbol(
    db_session: AsyncSession,
):
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("a1", "https://example.com/sym_a1",
             symbol="SYMAGG", published_at=now - timedelta(hours=1)),
        _row("a2", "https://example.com/sym_a2",
             symbol="SYMAGG", published_at=now - timedelta(hours=2)),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.symbol == "SYMAGG")
    )).all()
    rows[0].sentiment_score = 0.6
    rows[0].sentiment_label = "bullish"
    rows[0].sentiment_scored_at = now
    rows[1].sentiment_score = -0.5
    rows[1].sentiment_label = "bearish"
    rows[1].sentiment_scored_at = now
    await db_session.commit()

    out = await news_sentiment_service.read_symbol_sentiment(
        db_session, market="TW", symbol="SYMAGG", limit=10, max_age_hours=24,
    )
    assert out is not None
    assert out["bullish"] == 1
    assert out["bearish"] == 1
    assert out["count"] == 2
    assert round(out["avg_score"], 2) == 0.05


# ── daily LLM call cap (C7) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_score_pending_respects_daily_cap(db_session: AsyncSession):
    """Once SENTIMENT_DAILY_LLM_CALL_CAP is hit, no further LLM calls are
    made and `cap_hit=1` is returned in the result."""
    import services.news_sentiment_service as nss

    await insert_news_articles(db_session, [
        _row(f"capped news {i}",
             f"https://example.com/cap_{i}",
             symbol=f"CAPSYM_{i}")
        for i in range(3)
    ])

    call_count = {"n": 0}

    async def _counting_stream(*_a, **_kw):
        call_count["n"] += 1
        yield {"type": "delta", "text": "[]"}

    # Force "no quota left" by patching _can_make_llm_call.
    async def _always_full(*_a, **_kw):
        return False

    with patch.object(nss, "_can_make_llm_call", side_effect=_always_full), \
         patch.object(nss, "stream_chat", side_effect=_counting_stream):
        result = await nss.score_pending(
            db=db_session, batch_size=10, max_batches=3,
        )

    assert call_count["n"] == 0
    assert result["cap_hit"] == 1
    assert result["batches"] == 0


@pytest.mark.asyncio
async def test_can_make_llm_call_fails_closed_on_redis_error():
    """Cap helper must return False when Redis is unavailable — without
    the counter we can't enforce the cap, so the safer behaviour is to
    skip the scoring pass rather than risk unbounded spending during a
    multi-hour Redis incident."""
    import services.news_sentiment_service as nss

    async def _broken_incr(*_a, **_kw):
        raise RuntimeError("redis down")

    with patch("services.news_sentiment_service.cache_incr",
               side_effect=_broken_incr):
        ok = await nss._can_make_llm_call()

    assert ok is False


@pytest.mark.asyncio
async def test_score_pending_records_usage_when_provider_emits_it(
    db_session: AsyncSession,
):
    """Verifies the sentiment scorer writes an llm_usage_events row tagged
    `_system:news_sentiment` when the stream includes a usage event —
    so admins see background-task cost in UsageCard."""
    import services.news_sentiment_service as nss
    from models.llm_usage_event import LLMUsageEvent

    await insert_news_articles(db_session, [
        _row("usage tracking news",
             "https://example.com/usage_t",
             symbol="USAGE_SYM"),
    ])
    inserted = await db_session.scalar(
        select(NewsArticle).where(NewsArticle.symbol == "USAGE_SYM")
    )

    async def _stream_with_usage(*_a, **_kw):
        yield {"type": "delta",
               "text": f'[{{"id": {inserted.id}, "score": 0.5}}]'}
        yield {"type": "usage", "prompt_tokens": 120, "completion_tokens": 30}

    with patch.object(nss, "stream_chat", side_effect=_stream_with_usage):
        await nss.score_pending(
            db=db_session, batch_size=10, max_batches=1,
            provider="openai", model="gpt-4o-mini",
        )

    rows = (await db_session.scalars(
        select(LLMUsageEvent).where(
            LLMUsageEvent.persona_id == "_system:news_sentiment"
        )
    )).all()
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 120
    assert rows[0].completion_tokens == 30


@pytest.mark.asyncio
async def test_read_recent_market_sentiment_skips_unscored_rows(
    db_session: AsyncSession,
):
    """Aggregation must filter out rows whose `sentiment_score IS NULL`.
    The discussion orchestrator depends on this — otherwise an unscored
    bullish-toned headline would silently distort context counts.

    Rows are intentionally inserted with `published_at = 60 hours ago`
    and queried with `max_age_hours=72` — the wide window includes them
    but other tests in this file query with the default 24-h window so
    these rows don't pollute the downstream `..._aggregates` test.
    """
    now = datetime.now(UTC)
    old = now - timedelta(hours=60)
    await insert_news_articles(db_session, [
        # Scored row — should appear in the aggregation.
        _row("scored 1", "https://example.com/skip_scored", symbol=None,
             published_at=old),
        # Unscored row — should be filtered out by the IS NOT NULL guard.
        _row("unscored", "https://example.com/skip_unscored", symbol=None,
             published_at=old - timedelta(hours=1)),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like("https://example.com/skip_%"))
    )).all()
    by_link = {r.link: r for r in rows}
    by_link["https://example.com/skip_scored"].sentiment_score = 0.6
    by_link["https://example.com/skip_scored"].sentiment_label = "bullish"
    by_link["https://example.com/skip_scored"].sentiment_scored_at = now
    await db_session.commit()

    out = await news_sentiment_service.read_recent_market_sentiment(
        db_session, market="TW", limit=20, max_age_hours=72,
    )
    titles = [h["title"] for h in out["headlines"]]
    assert "scored 1" in titles
    assert "unscored" not in titles


@pytest.mark.asyncio
async def test_read_recent_market_sentiment_includes_symbol_tagged_rows(
    db_session: AsyncSession,
):
    """Regression: Google News TW headlines almost always include a 4-6
    digit stock code, and `ingest_news_tw`'s symbol-tagger writes that
    code into `NewsArticle.symbol`. A previous `symbol IS NULL` filter
    was excluding ~99% of TW news from the market-wide aggregator,
    leaving the discussion orchestrator with empty `news_sentiment`.
    Each headline now contributes regardless of symbol; the headline
    payload still carries `symbol` so the persona can disambiguate
    "2330 法說" from a broad-market story."""
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("台積電(2330) 法說會優於預期", "https://example.com/tag_a",
             symbol="2330", published_at=now - timedelta(hours=1)),
        _row("外資連 3 日賣超台股", "https://example.com/tag_b",
             symbol=None, published_at=now - timedelta(hours=2)),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like("https://example.com/tag_%"))
    )).all()
    for r in rows:
        r.sentiment_score = 0.5
        r.sentiment_label = "bullish"
        r.sentiment_scored_at = now
    await db_session.commit()

    out = await news_sentiment_service.read_recent_market_sentiment(
        db_session, market="TW", limit=10, max_age_hours=24,
    )
    titles = [h["title"] for h in out["headlines"]]
    assert "台積電(2330) 法說會優於預期" in titles
    assert "外資連 3 日賣超台股" in titles
    assert out["bullish"] == 2
    # `symbol` field is preserved per-headline so the LLM can tell
    # which is per-stock and which is broad-market.
    by_title = {h["title"]: h for h in out["headlines"]}
    assert by_title["台積電(2330) 法說會優於預期"]["symbol"] == "2330"
    assert by_title["外資連 3 日賣超台股"]["symbol"] is None


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
    assert out["considered"] == 3
    assert round(out["avg_score"], 2) == 0.10
    assert len(out["headlines"]) == 3


@pytest.mark.asyncio
async def test_read_recent_market_sentiment_aggregates_full_window_not_just_sample(
    db_session: AsyncSession,
):
    """PR #214 fix: `avg_score` + bullish/bearish/neutral counts must
    reflect EVERY scored article in the time window, not just the
    `limit`-capped headlines slice. A 100-bullish-day no longer looks
    like "10 articles, 7 bullish" because the headline cap stopped
    early."""
    now = datetime.now(UTC)
    bulk = []
    for i in range(15):
        bulk.append(_row(
            f"bullish-{i}", f"https://example.com/win_b_{i}",
            symbol=None, published_at=now - timedelta(hours=i),
        ))
    for i in range(5):
        bulk.append(_row(
            f"bearish-{i}", f"https://example.com/win_x_{i}",
            symbol=None, published_at=now - timedelta(hours=i),
        ))
    await insert_news_articles(db_session, bulk)
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like("https://example.com/win_%"))
    )).all()
    for r in rows:
        is_bullish = r.title.startswith("bullish")
        r.sentiment_score = 0.7 if is_bullish else -0.5
        r.sentiment_label = "bullish" if is_bullish else "bearish"
        r.sentiment_scored_at = now
    await db_session.commit()

    # limit=5 cuts the headline sample to 5; the bug used to make
    # avg_score / bullish / bearish reflect only those 5. Now they
    # must reflect all 20 — 15 bullish + 5 bearish.
    out = await news_sentiment_service.read_recent_market_sentiment(
        db_session, market="TW", limit=5, max_age_hours=24,
    )
    assert out["considered"] == 20
    assert out["bullish"] == 15
    assert out["bearish"] == 5
    assert out["neutral"] == 0
    # avg = (15*0.7 + 5*-0.5) / 20 = 0.4
    assert round(out["avg_score"], 2) == 0.40
    # Headlines slice is still capped at the request.
    assert len(out["headlines"]) == 5


@pytest.mark.asyncio
async def test_read_symbol_sentiment_aggregates_full_window_not_just_sample(
    db_session: AsyncSession,
):
    """Mirror of the market-sentiment bug fix for the per-symbol
    reader. 12 scored articles for 2330 with 9 bullish: the old code
    showed "10 articles, 7 bullish" because of the headlines cap;
    fix makes it "considered=12, bullish=9" while keeping headlines
    capped at the request."""
    now = datetime.now(UTC)
    bulk = []
    for i in range(9):
        bulk.append(_row(
            f"sym-bull-{i}", f"https://example.com/sym_b_{i}",
            symbol="2330", published_at=now - timedelta(hours=i),
        ))
    for i in range(3):
        bulk.append(_row(
            f"sym-bear-{i}", f"https://example.com/sym_x_{i}",
            symbol="2330", published_at=now - timedelta(hours=i),
        ))
    await insert_news_articles(db_session, bulk)
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like("https://example.com/sym_%"))
    )).all()
    for r in rows:
        is_bullish = "bull" in r.title
        r.sentiment_score = 0.6 if is_bullish else -0.4
        r.sentiment_label = "bullish" if is_bullish else "bearish"
        r.sentiment_scored_at = now
    await db_session.commit()

    out = await news_sentiment_service.read_symbol_sentiment(
        db_session, market="TW", symbol="2330",
        limit=5, max_age_hours=24,
    )
    assert out is not None
    assert out["considered"] == 12
    assert out["bullish"] == 9
    assert out["bearish"] == 3
    assert len(out["headlines"]) == 5


@pytest.mark.asyncio
async def test_read_symbol_sentiment_default_window_is_seven_days(
    db_session: AsyncSession,
):
    """PR #214 default bumped 72h → 168h to surface mid-cap names
    that get one mention every few days. A 5-day-old article must
    now appear; a 9-day-old one must still age out."""
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("recent", "https://example.com/sym_w_a",
             symbol="2330", published_at=now - timedelta(days=5)),
        _row("ancient", "https://example.com/sym_w_b",
             symbol="2330", published_at=now - timedelta(days=9)),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like("https://example.com/sym_w_%"))
    )).all()
    for r in rows:
        r.sentiment_score = 0.5
        r.sentiment_label = "bullish"
        r.sentiment_scored_at = now
    await db_session.commit()

    out = await news_sentiment_service.read_symbol_sentiment(
        db_session, market="TW", symbol="2330",
    )
    assert out is not None
    titles = [h["title"] for h in out["headlines"]]
    assert "recent" in titles
    assert "ancient" not in titles
    assert out["considered"] == 1


# ── score_specific_articles (inline backfill scorer) ──────────────


@pytest.mark.asyncio
async def test_score_specific_articles_scores_only_listed_ids(
    db_session: AsyncSession,
):
    """Given an explicit ID list, score those articles inline. Other
    unscored articles in the same window are ignored — scope is the
    fixed list, not "all unscored rows" like the cron does."""
    await insert_news_articles(db_session, [
        _row("listed bullish", "https://example.com/inline_a",
             symbol="A_INLINE"),
        _row("listed bearish", "https://example.com/inline_b",
             symbol="B_INLINE"),
        _row("unlisted neutral", "https://example.com/inline_c",
             symbol="C_INLINE"),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.symbol.in_(
            ["A_INLINE", "B_INLINE", "C_INLINE"],
        )).order_by(NewsArticle.id)
    )).all()
    id_a, id_b, id_c = rows[0].id, rows[1].id, rows[2].id

    fake = (
        f'[{{"id": {id_a}, "score": 0.7, "reason": "x"}},'
        f' {{"id": {id_b}, "score": -0.5, "reason": "y"}}]'
    )
    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text(fake),
    ):
        result = await news_sentiment_service.score_specific_articles(
            db_session, article_ids=[id_a, id_b],
        )

    assert result["considered"] == 2
    assert result["scored"] == 2
    assert result["batches"] == 1
    assert result["cap_hit"] == 0

    # Listed articles got scored.
    refreshed = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.id.in_([id_a, id_b, id_c]))
        .order_by(NewsArticle.id)
    )).all()
    assert refreshed[0].sentiment_score == 0.7
    assert refreshed[1].sentiment_score == -0.5
    # Unlisted article was not touched — confirms scope is the ID list,
    # not "any unscored row in the same window".
    assert refreshed[2].sentiment_score is None
    assert refreshed[2].sentiment_scored_at is None


@pytest.mark.asyncio
async def test_score_specific_articles_skips_already_scored(
    db_session: AsyncSession,
):
    """Race with cron: an ID in the list may have been scored between
    when the caller built the list and now. Re-fetch should skip
    already-scored rows so we don't double-spend the interactive
    budget."""
    now = datetime.now(UTC)
    await insert_news_articles(db_session, [
        _row("already scored", "https://example.com/race_a",
             symbol="RACE_A"),
        _row("still pending", "https://example.com/race_b",
             symbol="RACE_B"),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.symbol.in_(
            ["RACE_A", "RACE_B"],
        )).order_by(NewsArticle.id)
    )).all()
    id_a, id_b = rows[0].id, rows[1].id

    # Pretend the cron already scored RACE_A.
    rows[0].sentiment_score = 0.4
    rows[0].sentiment_label = "bullish"
    rows[0].sentiment_scored_at = now
    await db_session.commit()

    fake = f'[{{"id": {id_b}, "score": -0.3, "reason": "z"}}]'
    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text(fake),
    ):
        result = await news_sentiment_service.score_specific_articles(
            db_session, article_ids=[id_a, id_b],
        )

    # Only RACE_B was actually scored (RACE_A skipped).
    assert result["considered"] == 1
    assert result["scored"] == 1

    refreshed = await db_session.get(NewsArticle, id_a)
    # Cron's score preserved, not overwritten.
    assert refreshed.sentiment_score == 0.4


@pytest.mark.asyncio
async def test_score_specific_articles_empty_list_returns_zeros(
    db_session: AsyncSession,
):
    """Empty input: no DB hits, no LLM call, return zero counters.
    Used by the backfill orchestrator when nothing was inserted."""
    result = await news_sentiment_service.score_specific_articles(
        db_session, article_ids=[],
    )
    assert result == {
        "considered": 0, "scored": 0,
        "batches": 0, "cap_hit": 0,
    }


@pytest.mark.asyncio
async def test_score_specific_articles_respects_interactive_cap(
    db_session: AsyncSession,
):
    """Interactive cap exhausts → loop breaks, returns cap_hit=1.
    Verifies the cap counter is the SEPARATE one
    (`sentiment:interactive_llm_calls`), not the cron's daily."""
    await insert_news_articles(db_session, [
        _row(f"cap test {i}", f"https://example.com/cap_{i}",
             symbol=f"CAP_{i}")
        for i in range(3)
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like(
            "https://example.com/cap_%",
        )).order_by(NewsArticle.id)
    )).all()
    ids = [r.id for r in rows]

    # Cap returns False → no batches run.
    with patch(
        "services.news_sentiment_service._can_make_interactive_llm_call",
        return_value=False,
    ):
        result = await news_sentiment_service.score_specific_articles(
            db_session, article_ids=ids,
        )

    assert result["batches"] == 0
    assert result["scored"] == 0
    assert result["cap_hit"] == 1


# ── select_unscored_in_window ─────────────────────────────────────


@pytest.mark.asyncio
async def test_select_unscored_in_window_returns_only_unscored(
    db_session: AsyncSession,
):
    """The reader returns IDs of only NULL-`sentiment_scored_at` rows
    in the date window, newest-first. Used by news_backfill_service to
    pick the just-inserted rows for inline scoring."""
    base = datetime(2024, 6, 10, 12, 0, tzinfo=UTC)
    await insert_news_articles(db_session, [
        _row("in-window unscored old",
             "https://example.com/win_a",
             symbol="WIN_A", published_at=base),
        _row("in-window unscored new",
             "https://example.com/win_b",
             symbol="WIN_B", published_at=base + timedelta(hours=4)),
        _row("in-window scored",
             "https://example.com/win_c",
             symbol="WIN_C", published_at=base + timedelta(hours=2)),
        _row("out-of-window",
             "https://example.com/win_d",
             symbol="WIN_D", published_at=base + timedelta(days=60)),
    ])
    rows = (await db_session.scalars(
        select(NewsArticle).where(NewsArticle.link.like(
            "https://example.com/win_%",
        )).order_by(NewsArticle.id)
    )).all()
    a, b, c, _d = rows[0], rows[1], rows[2], rows[3]
    # Mark C as scored.
    c.sentiment_score = 0.0
    c.sentiment_label = "neutral"
    c.sentiment_scored_at = datetime.now(UTC)
    await db_session.commit()

    out = await news_sentiment_service.select_unscored_in_window(
        db_session, market="TW",
        start=base - timedelta(days=1),
        end=base + timedelta(days=1),
        limit=10,
    )

    # Newest-first: B (hour 4) before A (hour 0). C excluded (scored).
    # D excluded (out of window).
    assert out == [b.id, a.id]


@pytest.mark.asyncio
async def test_select_unscored_in_window_respects_limit(
    db_session: AsyncSession,
):
    base = datetime(2024, 6, 10, 12, 0, tzinfo=UTC)
    await insert_news_articles(db_session, [
        _row(f"limit test {i}",
             f"https://example.com/lim_{i}",
             symbol=f"LIM_{i}",
             published_at=base + timedelta(minutes=i))
        for i in range(5)
    ])
    out = await news_sentiment_service.select_unscored_in_window(
        db_session, market="TW",
        start=base - timedelta(hours=1),
        end=base + timedelta(hours=1),
        limit=3,
    )
    assert len(out) == 3
