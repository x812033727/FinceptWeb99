"""Tests for tasks.ingest_news_tw — the hourly TW news job.

After PR #128 the source switched from FinMind's `TaiwanStockNews`
(paid-only) to Google News RSS zh-TW. Tests reflect the new shape:
mock `google_news_tw.get_news` instead of `finmind.get_news`, error
formatter has Google-News-specific HTTP hints, and the article rows
write `source="google_news_tw"`.
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.news_article import NewsArticle
from services.ingest.repository import IngestHealth


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.ingest_news_tw.AsyncSessionLocal", return_value=_CM()):
        yield


def _gn_item(idx: int, *, symbol: str | None = None) -> dict:
    """Build a connector-shaped news dict mirroring what
    `google_news_tw.get_news()` returns."""
    return {
        "title":        f"Headline IT{idx}",
        "link":         f"https://news.google.com/articles/it{idx}",
        "source_name":  "鉅亨網",
        "description":  "summary text",
        # ISO 8601 UTC — connector returns this format from RFC 822 input.
        "published_at": "2026-04-28T02:00:00+00:00",
        "symbol":       symbol,
    }


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=False)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.google_news_tw.get_news", AsyncMock()) as gn:
        await ingest_news_tw.run()

    gn.assert_not_awaited()


@pytest.mark.asyncio
async def test_upstream_failure_records_unhealthy(patch_session):
    """Hard exception → record_health(ok=False) with formatted detail
    + failure-bookkeeping side-effect."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.record_failure",
               AsyncMock(return_value=1)) as failures, \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock(side_effect=RuntimeError("rss 503"))), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    failures.assert_awaited_once()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "rss 503" in kwargs["error"]
    assert "failure #1" in kwargs["error"]


@pytest.mark.asyncio
async def test_empty_result_records_ok_zero(patch_session):
    """Empty RSS result → mark healthy with row_count=0."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock(return_value=[])), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_success_inserts_rows(patch_session, db_session: AsyncSession):
    from tasks import ingest_news_tw

    items = [
        _gn_item(101, symbol=None),       # market-wide
        _gn_item(102, symbol="2330"),     # per-symbol
    ]

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()) as cleared, \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock(return_value=items)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    cleared.assert_awaited_once()

    rows = (await db_session.scalars(
        select(NewsArticle).where(
            NewsArticle.link.in_([
                "https://news.google.com/articles/it101",
                "https://news.google.com/articles/it102",
            ])
        )
    )).all()
    assert len(rows) == 2

    by_link = {r.link: r for r in rows}
    market_wide = by_link["https://news.google.com/articles/it101"]
    assert market_wide.symbol is None
    assert market_wide.source == "google_news_tw"

    per_sym = by_link["https://news.google.com/articles/it102"]
    assert per_sym.symbol == "2330"

    # ISO 8601 UTC input → stored as the same UTC instant. SQLite
    # drops tzinfo on read so we normalise before comparison.
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

    items = [_gn_item(201, symbol="2330")]

    common = (
        patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)),
        patch("tasks.ingest_news_tw.release_lock", AsyncMock()),
        patch("tasks.ingest_news_tw.backoff_remaining_seconds", AsyncMock(return_value=0)),
        patch("tasks.ingest_news_tw.clear_failures", AsyncMock()),
        patch("tasks.ingest_news_tw.google_news_tw.get_news",
              AsyncMock(return_value=items)),
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
        select(NewsArticle).where(
            NewsArticle.link == "https://news.google.com/articles/it201",
        )
    )).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_blank_published_at_drops_article_and_counts_it(
    patch_session, db_session: AsyncSession,
):
    """Articles with unparseable `pubDate` must be dropped, not stamped
    with `datetime.now(UTC)`. Backtest mode (`gather_market_context
    (as_of=...)`) was matching against `published_at`, so the old
    fallback poisoned the archive: every parse-fail article carried an
    ingestion-time timestamp forever and showed up in every backtest
    window indistinguishable from live news.

    The dropped count is surfaced in the health row's `error` field
    so a Google pubDate-format change spike doesn't silently degrade
    to "ok / 0" with no signal that work was discarded."""
    from tasks import ingest_news_tw

    items = [
        # Real headline — keeps.
        {
            "title":        "Valid headline",
            "link":         "https://news.google.com/articles/valid",
            "source_name":  "鉅亨網",
            "description":  "",
            "published_at": "2026-05-01T10:00:00+00:00",
            "symbol":       "2330",
        },
        # Blank pubdate — drops, counts.
        {
            "title":        "No date headline",
            "link":         "https://news.google.com/articles/nodate",
            "source_name":  "鉅亨網",
            "description":  "",
            "published_at": "",
            "symbol":       "2330",
        },
        # Garbage pubdate — drops, counts.
        {
            "title":        "Garbage date headline",
            "link":         "https://news.google.com/articles/garbage",
            "source_name":  "鉅亨網",
            "description":  "",
            "published_at": "not-a-date-at-all",
            "symbol":       "2330",
        },
    ]

    health_mock = AsyncMock()
    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock(return_value=items)), \
         patch("tasks.ingest_news_tw.record_health", health_mock):
        await ingest_news_tw.run()

    # Only the valid row survives.
    rows = (await db_session.scalars(
        select(NewsArticle).where(
            NewsArticle.link.like("https://news.google.com/articles/%"),
        )
    )).all()
    assert {r.link for r in rows} == {"https://news.google.com/articles/valid"}

    # Health record exposes the drop count so an admin can see the
    # parse-fail spike instead of just "row_count=1".
    kwargs = health_mock.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 1
    assert "fetched=3" in kwargs["error"]
    assert "attempted=1" in kwargs["error"]
    assert "dropped_pubdate=2" in kwargs["error"]


# ── error formatting ──────────────────────────────────────────────


def _http_error(status: int):
    """Build an httpx.HTTPStatusError so the formatter has all the
    fields it would see in production."""
    import httpx
    request = httpx.Request("GET", "https://news.google.com/rss/search")
    response = httpx.Response(status_code=status, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response,
    )


def test_format_news_error_429_mentions_rate_limit():
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(_http_error(429))
    assert "429" in msg
    assert "rate-limit" in msg.lower()


def test_format_news_error_503_mentions_unavailable():
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(_http_error(503))
    assert "503" in msg
    assert "unavailable" in msg.lower()


def test_format_news_error_403_mentions_blocked():
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(_http_error(403))
    assert "403" in msg
    assert ("blocked" in msg.lower() or "refused" in msg.lower())


def test_format_news_error_unknown_status_falls_through():
    """A status code without a hint still produces a readable message."""
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(_http_error(418))
    assert "418" in msg


def test_format_news_error_timeout():
    import httpx
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(httpx.ReadTimeout("read timeout"))
    assert "timeout" in msg.lower()


def test_format_news_error_connect():
    import httpx
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(httpx.ConnectError("dns failure"))
    assert "connect" in msg.lower()


def test_format_news_error_generic_exception():
    from tasks.ingest_news_tw import _format_news_error

    msg = _format_news_error(RuntimeError("something else"))
    assert "unexpected" in msg
    assert "something else" in msg


# ── backoff integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_work_when_backoff_active(patch_session):
    """When backoff TTL > 0, the task records a 'skipped' health entry
    and never calls the upstream."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=3600)), \
         patch("tasks.ingest_news_tw.get_failure_count",
               AsyncMock(return_value=2)), \
         patch("tasks.ingest_news_tw.get_health",
               AsyncMock(return_value=None)), \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock()) as gn, \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    gn.assert_not_awaited()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped" in kwargs["error"]
    assert "2 failures" in kwargs["error"]


@pytest.mark.asyncio
async def test_backoff_skip_preserves_last_real_error(patch_session):
    """The admin UI should keep showing the root cause while backoff
    counts down, instead of replacing it with a generic skipped message."""
    from tasks import ingest_news_tw

    previous = IngestHealth(
        job_id="ingest_news_tw",
        last_run_at="2026-04-30T00:00:00+00:00",
        ok=False,
        row_count=0,
        error="HTTP 429 Too Many Requests (Google News rate-limit)",
    )

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=7200)), \
         patch("tasks.ingest_news_tw.get_failure_count",
               AsyncMock(return_value=3)), \
         patch("tasks.ingest_news_tw.get_health",
               AsyncMock(return_value=previous)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    err = health.await_args.kwargs["error"]
    assert "skipped" in err
    assert "3 failures" in err
    assert "HTTP 429" in err


@pytest.mark.asyncio
async def test_backoff_skip_doesnt_recurse_on_skipped_error(patch_session):
    """If the previous health row was itself a 'skipped' entry, don't
    quote it as the 'last' error — that recurses skip → skip → skip."""
    from tasks import ingest_news_tw

    previous = IngestHealth(
        job_id="ingest_news_tw",
        last_run_at="2026-04-30T00:00:00+00:00",
        ok=False,
        row_count=0,
        error="skipped (backoff after 3 failures, ~120 min remaining; last: HTTP 429 ...)",
    )

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=7200)), \
         patch("tasks.ingest_news_tw.get_failure_count",
               AsyncMock(return_value=3)), \
         patch("tasks.ingest_news_tw.get_health",
               AsyncMock(return_value=previous)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    err = health.await_args.kwargs["error"]
    # Should not have nested "last: skipped..." string.
    assert err.count("skipped") == 1


@pytest.mark.asyncio
async def test_http_429_records_formatted_error_and_arms_backoff(patch_session):
    """End-to-end: HTTP 429 from Google News → formatted error string +
    record_failure called once."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.record_failure",
               AsyncMock(return_value=3)) as record_fail, \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()) as cleared, \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock(side_effect=_http_error(429))), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    record_fail.assert_awaited_once()
    cleared.assert_not_awaited()
    err = health.await_args.kwargs["error"]
    assert "429" in err
    assert "rate-limit" in err.lower()
    assert "failure #3" in err


@pytest.mark.asyncio
async def test_success_clears_backoff_state(patch_session, db_session: AsyncSession):
    """After repeated failures armed a backoff, a successful run must
    reset the failure counter so the task isn't throttled forever."""
    from tasks import ingest_news_tw

    items = [_gn_item(401, symbol=None)]

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.record_failure", AsyncMock()) as record_fail, \
         patch("tasks.ingest_news_tw.clear_failures",
               AsyncMock()) as cleared, \
         patch("tasks.ingest_news_tw.google_news_tw.get_news",
               AsyncMock(return_value=items)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()):
        await ingest_news_tw.run()

    cleared.assert_awaited_once()
    record_fail.assert_not_awaited()


# ── repository.backoff helpers (unit) ─────────────────────────────


def test_backoff_seconds_for_curve():
    from services.ingest.repository import _backoff_seconds_for

    assert _backoff_seconds_for(0) == 0
    assert _backoff_seconds_for(1) == 3600
    assert _backoff_seconds_for(2) == 7200
    assert _backoff_seconds_for(3) == 14400
    # Cap at 6 h once 2^(N-1) * 3600 >= 6h * 3600
    assert _backoff_seconds_for(4) == 6 * 3600
    assert _backoff_seconds_for(99) == 6 * 3600
