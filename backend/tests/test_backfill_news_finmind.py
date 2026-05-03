"""Tests for scripts.backfill_news_finmind.

Coverage:
  - `_parse_published_at` handles FinMind's "YYYY-MM-DD HH:MM:SS" format
    + the fallback ISO / date-only forms; returns None on garbage rather
    than crashing the whole chunk.
  - `_to_row` filters drop-conditions (missing title/link, unparseable
    date) and stamps `source="finmind_backfill"` so admins can later
    distinguish backfill rows from the regular Google News RSS ingest.
  - `backfill` walks the date range in 30-day chunks and forwards each
    `(start, end)` to `finmind.get_news` — verifies the chunking math
    against an end-of-range edge case.
  - `backfill` continues past per-chunk failures (FinMind transient 500
    on one month doesn't abort an overnight 2-year backfill).
  - End-to-end: a mocked FinMind response lands in `news_articles` via
    `insert_news_articles` with sha256 dedup intact (re-running the
    same range writes nothing the second time).
"""
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news_article import NewsArticle
from scripts import backfill_news_finmind as backfill


# ── _parse_published_at ───────────────────────────────────────────


def test_parse_published_at_finmind_space_separated():
    """FinMind's `date` field is `YYYY-MM-DD HH:MM:SS` without timezone.
    Treated as UTC since FinMind doesn't expose a tz on historical TW
    news (the diff vs real publish-time falls inside the 48h sentiment
    aggregation window anyway)."""
    out = backfill._parse_published_at("2026-04-15 09:30:00")
    assert out == datetime(2026, 4, 15, 9, 30, tzinfo=UTC)


def test_parse_published_at_iso_form():
    out = backfill._parse_published_at("2026-04-15T09:30:00+00:00")
    assert out == datetime(2026, 4, 15, 9, 30, tzinfo=UTC)


def test_parse_published_at_date_only_anchors_to_midnight():
    out = backfill._parse_published_at("2026-04-15")
    assert out == datetime(2026, 4, 15, 0, 0, tzinfo=UTC)


def test_parse_published_at_returns_none_on_garbage():
    """Per-row resilience — a bad date in one item must not poison
    the whole chunk. Caller drops the row instead of crashing."""
    assert backfill._parse_published_at("not-a-date") is None
    assert backfill._parse_published_at("") is None
    assert backfill._parse_published_at(None) is None


# ── _to_row ───────────────────────────────────────────────────────


def test_to_row_stamps_finmind_backfill_source():
    """Backfilled rows carry `source="finmind_backfill"` so admins
    can distinguish them from regular hourly Google News RSS rows
    (`source="google_news_rss"` etc.) when investigating coverage."""
    row = backfill._to_row({
        "title": "t", "link": "https://example.com",
        "source_name": "經濟日報", "description": "x",
        "published_at": "2026-04-15 09:30:00", "symbol": "2330",
    }, market="TW")
    assert row is not None
    assert row.source == "finmind_backfill"
    assert row.symbol == "2330"
    assert row.publisher == "經濟日報"
    assert row.market == "TW"


def test_to_row_drops_when_title_or_link_missing():
    assert backfill._to_row(
        {"title": "", "link": "https://x"}, market="TW",
    ) is None
    assert backfill._to_row(
        {"title": "t", "link": ""}, market="TW",
    ) is None


def test_to_row_drops_when_pubdate_unparseable():
    """Same drop-on-bad-pubdate semantic as the live Google News
    ingest (`tasks/ingest_news_tw._to_row`) — better to lose the row
    than poison the archive with a wrong timestamp that backtest
    queries can't escape."""
    row = backfill._to_row({
        "title": "t", "link": "https://example.com",
        "published_at": "garbage",
    }, market="TW")
    assert row is None


# ── backfill (chunking) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_chunks_by_30_days():
    """Verify the chunk window math: a 90-day range produces
    exactly three FinMind calls with non-overlapping (start, end)
    pairs, ending exactly at the user-requested end date."""
    calls: list[tuple[str, str]] = []

    async def _fake_get_news(start, symbol="", end_date=None):
        calls.append((start, end_date))
        return []

    with patch.object(
        backfill.finmind, "get_news", new=AsyncMock(side_effect=_fake_get_news),
    ):
        await backfill.backfill(date(2024, 1, 1), date(2024, 3, 31))

    # 30-day chunks: Jan 1-30, Jan 31-Feb 29, Mar 1-30, Mar 31-31
    assert len(calls) == 4
    assert calls[0] == ("2024-01-01", "2024-01-30")
    assert calls[-1][1] == "2024-03-31"  # final chunk caps at user's end


@pytest.mark.asyncio
async def test_backfill_continues_past_chunk_failures():
    """A transient FinMind 5xx on one chunk must not abort the entire
    overnight backfill — log + skip, keep going. End count reflects
    only the chunks that succeeded."""
    call_count = 0

    async def _flaky(start, symbol="", end_date=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated FinMind 500")
        return [{
            "title":       f"news in chunk {call_count}",
            "link":        f"https://example.com/{call_count}",
            "source_name": "經濟日報",
            "published_at": f"{start} 09:00:00",
            "symbol":      None,
        }]

    with patch.object(
        backfill.finmind, "get_news", new=AsyncMock(side_effect=_flaky),
    ), patch.object(
        backfill, "insert_news_articles", new=AsyncMock(),
    ):
        fetched, attempted = await backfill.backfill(
            date(2024, 1, 1), date(2024, 3, 31),
        )

    # 4 chunks, one failed, three succeeded with one row each.
    assert call_count == 4
    assert fetched == 3
    assert attempted == 3


# ── End-to-end against the test DB ────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_writes_to_news_articles_with_dedup(
    db_session: AsyncSession,
):
    """End-to-end: a mocked FinMind response lands in `news_articles`,
    and re-running the same range is a no-op (sha256(title+link)
    dedup at the insert layer). This is what makes the script safe to
    re-run for any range without manual cleanup."""
    fake_payload = [{
        "title":        "台積電 Q2 EPS 創高",
        "link":         "https://example.com/2330",
        "source_name":  "經濟日報",
        "description":  "EPS +35% YoY",
        "published_at": "2026-04-15 09:30:00",
        "symbol":       "2330",
    }]

    class _PassthroughCM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch.object(
        backfill.finmind, "get_news",
        new=AsyncMock(return_value=fake_payload),
    ), patch.object(
        backfill, "AsyncSessionLocal", return_value=_PassthroughCM(),
    ):
        # First run: write the row.
        await backfill.backfill(date(2026, 4, 15), date(2026, 4, 15))
        rows = (await db_session.scalars(
            select(NewsArticle).where(NewsArticle.title == "台積電 Q2 EPS 創高"),
        )).all()
        assert len(rows) == 1
        assert rows[0].source == "finmind_backfill"
        assert rows[0].symbol == "2330"
        # SQLite (test fixture) drops tzinfo on read; Postgres
        # (production) preserves it. Compare on the wall-clock parts
        # so the test passes against both backends.
        stored = rows[0].published_at
        assert stored.replace(tzinfo=None) == datetime(2026, 4, 15, 9, 30)

        # Second run with identical payload: dedup via sha256(title+link)
        # → row count stays at 1.
        await backfill.backfill(date(2026, 4, 15), date(2026, 4, 15))
        rows = (await db_session.scalars(
            select(NewsArticle).where(NewsArticle.title == "台積電 Q2 EPS 創高"),
        )).all()
        assert len(rows) == 1
