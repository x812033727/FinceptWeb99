"""Unit tests for the announcement-side sentiment scoring (PR-D1b).

Mirrors `test_news_sentiment_service.py` but exercises the
`corporate_announcements` lane of `score_pending_announcements`.

Key invariants pinned here:
  - Announcement scoring shares the same daily-cap counter as news
    (no double-bookkeeping).
  - LLM call shape includes the category prefix in each row, since
    material-info disclosures need that framing to score correctly
    (a "減資" disclosure with a neutral-sounding title still
    warrants a bearish prior).
  - Stamp-on-fallback contract: every input row gets
    `sentiment_scored_at` updated even when the LLM returned
    nothing for it (otherwise the cron retries the same row
    forever).
  - LLM-error path leaves rows un-stamped (transient retry).
  - Daily-cap path skips the lane (no LLM call).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.corporate_announcement import CorporateAnnouncement
from services import news_sentiment_service
from services.ingest.repository import (
    CorporateAnnouncementRow,
    insert_corporate_announcements,
)


# ── helpers ──────────────────────────────────────────────────────


def _row(
    title: str,
    *,
    symbol: str = "2330",
    category: str = "財務報告",
    body: str | None = None,
    announced_at: datetime | None = None,
    dedup_hash: str | None = None,
) -> CorporateAnnouncementRow:
    return CorporateAnnouncementRow(
        market="TW",
        symbol=symbol,
        announced_at=announced_at or datetime.now(UTC),
        category=category,
        title=title,
        body=body,
        source_url=None,
        source="mops_t05st02",
        dedup_hash=dedup_hash or f"h-ann-{symbol}-{title[:20]}",
    )


def _stream_text(text: str):
    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": text}
    return _gen


def _stream_error(message: str):
    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "error", "message": message}
    return _gen


# ── _format_announcement_items prompt shape ──────────────────────


def test_format_announcement_items_includes_category_prefix():
    """Material-info LLM scoring needs `(category)` in the title so
    the model has the disclosure-type framing without us having to
    spell out "this is a 重大訊息" in the prompt template."""
    rows = [
        CorporateAnnouncement(
            id=42, market="TW", symbol="2330",
            announced_at=datetime.now(UTC),
            category="減資", title="本公司減資公告",
            body=None, source_url=None,
            source="mops", dedup_hash="h",
        ),
    ]
    out = news_sentiment_service._format_announcement_items(rows)
    assert '"id": 42' in out
    assert "(減資)" in out
    assert "[2330]" in out


def test_format_announcement_items_handles_blank_category():
    """Defensive: connector falls back to '未分類' for missing
    categories, but a downstream change shouldn't crash the
    formatter on empty/None either."""
    rows = [
        CorporateAnnouncement(
            id=1, market="TW", symbol="2330",
            announced_at=datetime.now(UTC),
            category="", title="title",
            body=None, source_url=None,
            source="mops", dedup_hash="h",
        ),
    ]
    out = news_sentiment_service._format_announcement_items(rows)
    assert '"id": 1' in out
    assert '[2330]' in out


# ── score_pending_announcements end-to-end ───────────────────────


@pytest.mark.asyncio
async def test_score_pending_announcements_writes_back_scores(
    db_session: AsyncSession,
):
    await insert_corporate_announcements(db_session, [
        _row("台積電 113 年第一季合併財報",
             symbol="ANN_1", category="財務報告"),
        _row("聯發科減資公告",
             symbol="ANN_2", category="減資"),
    ])
    inserted = (await db_session.scalars(
        select(CorporateAnnouncement)
        .where(CorporateAnnouncement.symbol.in_(["ANN_1", "ANN_2"]))
        .order_by(CorporateAnnouncement.id)
    )).all()
    id1, id2 = inserted[0].id, inserted[1].id

    fake_response = (
        f'[{{"id": {id1}, "score": 0.7, "reason": "EPS 創高"}},'
        f' {{"id": {id2}, "score": -0.6, "reason": "減資"}}]'
    )
    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text(fake_response),
    ):
        result = await news_sentiment_service.score_pending_announcements(
            db=db_session, batch_size=10, max_batches=1,
        )

    assert result["considered"] >= 2
    assert result["scored"] >= 2

    refreshed = (await db_session.scalars(
        select(CorporateAnnouncement)
        .where(CorporateAnnouncement.id.in_([id1, id2]))
        .order_by(CorporateAnnouncement.id)
    )).all()
    a, b = refreshed[0], refreshed[1]
    assert a.sentiment_score == 0.7
    assert a.sentiment_label == "bullish"
    assert a.sentiment_scored_at is not None
    assert b.sentiment_score == -0.6
    assert b.sentiment_label == "bearish"


@pytest.mark.asyncio
async def test_score_pending_announcements_stamps_unscored_to_avoid_reprocessing(
    db_session: AsyncSession,
):
    """LLM returned [] → input rows still stamped so the next pass
    doesn't keep retrying the same headlines."""
    await insert_corporate_announcements(db_session, [
        _row("空 LLM 回應的公告", symbol="ANN_UNSCORED"),
    ])
    aid = (await db_session.scalar(
        select(CorporateAnnouncement)
        .where(CorporateAnnouncement.symbol == "ANN_UNSCORED")
    )).id

    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text("[]"),
    ):
        await news_sentiment_service.score_pending_announcements(
            db=db_session, batch_size=10, max_batches=1,
        )

    refreshed = await db_session.get(CorporateAnnouncement, aid)
    assert refreshed.sentiment_score is None
    assert refreshed.sentiment_label is None
    assert refreshed.sentiment_scored_at is not None  # but stamped


@pytest.mark.asyncio
async def test_score_pending_announcements_skips_when_all_scored(
    db_session: AsyncSession,
):
    """No unscored rows in window → no LLM call, no work."""
    # Pre-stamp every row to keep cross-test pollution out of the
    # `mocked.assert_not_called` check.
    from sqlalchemy import update as _update
    now = datetime.now(UTC)
    await db_session.execute(
        _update(CorporateAnnouncement)
        .where(CorporateAnnouncement.sentiment_scored_at.is_(None))
        .values(sentiment_scored_at=now)
    )
    await db_session.commit()

    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_text("[]"),
    ) as mocked:
        result = await news_sentiment_service.score_pending_announcements(
            db=db_session, batch_size=10, max_batches=2,
        )
    assert result["considered"] == 0
    assert result["scored"] == 0
    mocked.assert_not_called()


@pytest.mark.asyncio
async def test_score_pending_announcements_does_not_stamp_on_llm_error(
    db_session: AsyncSession,
):
    """Transient LLM failure → row stays unscored so the next pass
    can retry. Stamping on error would make a flaky LLM permanently
    poison the archive."""
    await insert_corporate_announcements(db_session, [
        _row("LLM 暫時失敗", symbol="ANN_TRANSIENT"),
    ])
    aid = (await db_session.scalar(
        select(CorporateAnnouncement)
        .where(CorporateAnnouncement.symbol == "ANN_TRANSIENT")
    )).id

    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_error("rate-limited; retry later"),
    ):
        result = await news_sentiment_service.score_pending_announcements(
            db=db_session, batch_size=10, max_batches=1,
        )

    refreshed = await db_session.get(CorporateAnnouncement, aid)
    assert refreshed.sentiment_scored_at is None
    assert "rate-limited" in (result.get("error") or "")


@pytest.mark.asyncio
async def test_score_pending_announcements_respects_daily_cap(
    db_session: AsyncSession,
):
    """When `_can_make_llm_call` returns False, the lane exits
    immediately with cap_hit=1 — sharing the same Redis counter as
    the news lane is the entire point of the cap mechanism."""
    await insert_corporate_announcements(db_session, [
        _row("被 cap 卡住的公告", symbol="ANN_CAPPED"),
    ])

    call_count = {"n": 0}

    async def _capped(*_a, **_kw):
        return False

    async def _counting_stream(*_a, **_kw) -> AsyncIterator[dict]:
        call_count["n"] += 1
        yield {"type": "delta", "text": "[]"}

    with patch(
        "services.news_sentiment_service._can_make_llm_call",
        side_effect=_capped,
    ), patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_counting_stream,
    ):
        result = await news_sentiment_service.score_pending_announcements(
            db=db_session, batch_size=10, max_batches=4,
        )
    assert result["cap_hit"] == 1
    assert result["scored"] == 0
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_score_pending_announcements_records_usage_under_separate_persona(
    db_session: AsyncSession,
):
    """Usage attribution lands under `_system:announcement_sentiment`
    so admins can split background-task cost between the two lanes
    in UsageCard."""
    await insert_corporate_announcements(db_session, [
        _row("有 usage 事件", symbol="ANN_USAGE"),
    ])
    inserted = await db_session.scalar(
        select(CorporateAnnouncement)
        .where(CorporateAnnouncement.symbol == "ANN_USAGE")
    )
    aid = inserted.id

    async def _stream_with_usage(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta",
               "text": f'[{{"id": {aid}, "score": 0.4, "reason": "ok"}}]'}
        yield {"type": "usage",
               "prompt_tokens": 123, "completion_tokens": 45}

    # `record_usage` is lazy-imported inside `_run_llm_score`, so patch
    # it at its source module (services.llm_usage_service) to intercept.
    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_stream_with_usage,
    ), patch(
        "services.llm_usage_service.record_usage", new_callable=AsyncMock,
    ) as record_mock:
        await news_sentiment_service.score_pending_announcements(
            db=db_session, batch_size=10, max_batches=1,
        )

    record_mock.assert_awaited()
    kwargs = record_mock.await_args.kwargs
    assert kwargs.get("persona_id") == "_system:announcement_sentiment"
    assert kwargs.get("prompt_tokens") == 123
    assert kwargs.get("completion_tokens") == 45


@pytest.mark.asyncio
async def test_fetch_unscored_announcements_excludes_stale_rows(
    db_session: AsyncSession,
):
    """Stale rows outside max_age_days are skipped — important for
    the cron's default 7-day lookback so a fresh backfill doesn't
    re-scan the entire archive every hour."""
    historical = datetime.now(UTC) - timedelta(days=100)
    fresh = datetime.now(UTC) - timedelta(hours=1)
    await insert_corporate_announcements(db_session, [
        _row("100 天前的公告", symbol="ANN_HIST",
             announced_at=historical, dedup_hash="h-stale"),
        _row("1 小時前的公告", symbol="ANN_FRESH",
             announced_at=fresh, dedup_hash="h-fresh"),
    ])

    rows = await news_sentiment_service._fetch_unscored_announcements(
        db_session, limit=10, max_age_days=7,
    )
    titles = [r.title for r in rows]
    assert "1 小時前的公告" in titles
    assert "100 天前的公告" not in titles
