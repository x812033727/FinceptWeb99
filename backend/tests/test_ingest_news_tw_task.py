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
         patch("tasks.ingest_news_tw.finmind.get_news",
               AsyncMock(side_effect=RuntimeError("finmind 503"))), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    failures.assert_awaited_once()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    # New format: "unexpected: finmind 503 (failure #1; auto-backoff armed)"
    assert "finmind 503" in kwargs["error"]
    assert "failure #1" in kwargs["error"]


@pytest.mark.asyncio
async def test_empty_result_records_ok_zero(patch_session):
    """Quota-exhausted FinMind returns []. The run should mark itself
    healthy with row_count=0 — the cron successfully executed."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()), \
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
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()) as cleared, \
         patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock(return_value=items)), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    # Successful run must clear any leftover backoff state.
    cleared.assert_awaited_once()

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
        patch("tasks.ingest_news_tw.backoff_remaining_seconds", AsyncMock(return_value=0)),
        patch("tasks.ingest_news_tw.clear_failures", AsyncMock()),
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
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()), \
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


# ── error formatting ──────────────────────────────────────────────


def _http_error(status: int, reason: str = "", body: dict | None = None):
    """Build a real httpx.HTTPStatusError so the formatter has all the
    fields it would see in production. `reason_phrase` is read-only and
    auto-derived from the status code, so the `reason` arg is unused."""
    import httpx

    del reason  # auto-derived from status by httpx
    request = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    response = httpx.Response(
        status_code=status,
        request=request,
        json=body if body is not None else {},
    )
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response,
    )


def test_format_finmind_error_401_includes_token_hint():
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(_http_error(401, "Unauthorized"))
    assert "401" in msg
    assert "FINMIND_TOKEN" in msg


def test_format_finmind_error_400_mentions_empty_token():
    """FinMind returns 400 (not 401) when the token is empty for paid
    datasets like TaiwanStockNews — confusing default behaviour, so
    our hint dict explicitly maps it to the FINMIND_TOKEN guidance."""
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(_http_error(400, "Bad Request"))
    assert "400" in msg
    assert "FINMIND_TOKEN" in msg


def test_format_finmind_error_402_mentions_paid_dataset():
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(_http_error(402, "Payment Required"))
    assert "402" in msg
    assert "paid" in msg.lower()


def test_format_finmind_error_429_mentions_quota():
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(_http_error(429, "Too Many Requests"))
    assert "429" in msg
    assert "quota" in msg.lower()


def test_format_finmind_error_includes_body_msg():
    """FinMind sometimes returns `{"msg": "...", "status": 401}` in the
    body — we surface the msg in the formatted error so operators see
    the upstream's own explanation."""
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(
        _http_error(401, "Unauthorized", body={"msg": "token is invalid"}),
    )
    assert "token is invalid" in msg


def test_format_finmind_error_timeout():
    import httpx
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(httpx.ReadTimeout("read timeout"))
    assert "timeout" in msg.lower()


def test_format_finmind_error_connect():
    import httpx
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(httpx.ConnectError("dns failure"))
    assert "connect" in msg.lower()


def test_format_finmind_error_generic_exception():
    from tasks.ingest_news_tw import _format_finmind_error

    msg = _format_finmind_error(RuntimeError("something else"))
    assert "unexpected" in msg
    assert "something else" in msg


# ── backoff integration ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_work_when_backoff_active(patch_session):
    """When backoff TTL > 0, the task records a 'skipped' health entry
    and never calls FinMind. Operators see the cooldown counting down
    in the admin UI's `last_run_at`."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=3600)), \
         patch("tasks.ingest_news_tw.get_failure_count",
               AsyncMock(return_value=2)), \
         patch("tasks.ingest_news_tw.finmind.get_news", AsyncMock()) as fm, \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    fm.assert_not_awaited()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "skipped" in kwargs["error"]
    assert "2 failures" in kwargs["error"]


@pytest.mark.asyncio
async def test_http_401_records_formatted_error_and_arms_backoff(patch_session):
    """End-to-end: HTTP 401 from FinMind → formatted error string +
    record_failure called once."""
    from tasks import ingest_news_tw

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.record_failure",
               AsyncMock(return_value=3)) as record_fail, \
         patch("tasks.ingest_news_tw.clear_failures", AsyncMock()) as cleared, \
         patch("tasks.ingest_news_tw.finmind.get_news",
               AsyncMock(side_effect=_http_error(401, "Unauthorized"))), \
         patch("tasks.ingest_news_tw.record_health", AsyncMock()) as health:
        await ingest_news_tw.run()

    record_fail.assert_awaited_once()
    cleared.assert_not_awaited()
    err = health.await_args.kwargs["error"]
    assert "401" in err
    assert "FINMIND_TOKEN" in err
    assert "failure #3" in err


@pytest.mark.asyncio
async def test_success_clears_backoff_state(patch_session, db_session: AsyncSession):
    """After repeated failures armed a backoff, a successful run must
    reset the failure counter so the task isn't throttled forever."""
    from tasks import ingest_news_tw

    items = [_finmind_item(401, symbol=None)]

    with patch("tasks.ingest_news_tw.acquire_lock", AsyncMock(return_value=True)), \
         patch("tasks.ingest_news_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_news_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_news_tw.record_failure", AsyncMock()) as record_fail, \
         patch("tasks.ingest_news_tw.clear_failures",
               AsyncMock()) as cleared, \
         patch("tasks.ingest_news_tw.finmind.get_news",
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
