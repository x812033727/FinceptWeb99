"""Per-provider failure backoff for the news sentiment scorer.

The previous "break on failure" loop guard already stopped a single
cron run from spending the full daily cap on a sustained provider
outage — but every NEXT hourly run reset, reserved one cap unit,
failed again, and broke. 24 hours of sustained 503s = 24 cap units
burned without a single row scored. The new per-provider cooldown
keys (`sentiment:cooldown:{provider}`) make subsequent ticks skip
the run entirely until the backoff TTL expires.

These tests pin the contract:
  - cooldown set ⇒ score_pending returns immediately, no cap reserved
  - failure ⇒ cooldown set with the right TTL per backoff schedule
  - success ⇒ cooldown + fail counter cleared
  - Redis outage on the cooldown check ⇒ fail-open (continue scoring)
    — safe because the daily cap counter already fails closed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import services.news_sentiment_service as nss
from datetime import UTC, datetime
from services.ingest.repository import NewsArticleRow, insert_news_articles


def _row(title: str, link: str, *, symbol: str = "2330") -> NewsArticleRow:
    return NewsArticleRow(
        market="TW",
        symbol=symbol,
        published_at=datetime.now(UTC),
        title=title,
        link=link,
        publisher="test",
        summary=None,
        payload=None,
        source="test",
    )


@pytest.mark.asyncio
async def test_is_provider_in_cooldown_returns_true_when_key_present():
    """Whenever Redis has the cooldown marker key, the provider is
    considered in cooldown — irrespective of TTL value."""
    async def _present(_key):
        return "1"
    with patch.object(nss, "cache_get", side_effect=_present):
        assert await nss._is_provider_in_cooldown("anthropic") is True


@pytest.mark.asyncio
async def test_is_provider_in_cooldown_returns_false_when_missing():
    """Missing key (TTL expired or never set) ⇒ provider OK to call."""
    async def _absent(_key):
        return None
    with patch.object(nss, "cache_get", side_effect=_absent):
        assert await nss._is_provider_in_cooldown("anthropic") is False


@pytest.mark.asyncio
async def test_is_provider_in_cooldown_fails_open_on_redis_error():
    """Redis outage on the cooldown check returns False so scoring
    continues — the daily cap counter (which fails closed) already
    bounds runaway spend in a Redis incident."""
    async def _boom(_key):
        raise RuntimeError("redis down")
    with patch.object(nss, "cache_get", side_effect=_boom):
        assert await nss._is_provider_in_cooldown("anthropic") is False


@pytest.mark.asyncio
async def test_record_provider_failure_uses_backoff_schedule():
    """Backoff schedule pins cooldown TTL based on the failure count.

    1 fail → 1h, 2 fail → 2h, 3 fail → 4h, 4+ fail → 6h cap.
    A single regression test pins all four steps so a future tweak
    of `_BACKOFF_SCHEDULE_HOURS` is intentional rather than silent.
    """
    cache_set_calls: list[tuple[str, str, int]] = []

    async def _record_set(key, value, ttl_seconds=None):
        cache_set_calls.append((key, value, ttl_seconds))

    schedule = [
        (1, 1 * 3600),
        (2, 2 * 3600),
        (3, 4 * 3600),
        (5, 6 * 3600),     # capped
        (99, 6 * 3600),    # still capped
    ]
    for fail_count, expected_ttl in schedule:
        cache_set_calls.clear()
        async def _stub_incr(*_a, **_kw):
            return fail_count
        with patch.object(nss, "cache_incr", side_effect=_stub_incr), \
             patch.object(nss, "cache_set", side_effect=_record_set):
            count = await nss._record_provider_failure(
                "anthropic", lane="news",
            )
        assert count == fail_count
        assert any(
            ttl == expected_ttl for (_k, _v, ttl) in cache_set_calls
        ), f"expected ttl {expected_ttl} for fail_count={fail_count}, got {cache_set_calls}"


@pytest.mark.asyncio
async def test_record_provider_failure_treats_redis_error_as_first_fail():
    """When the failure counter incr raises, `_record_provider_failure`
    falls back to count=1 → 1h cooldown. Better than letting the cron
    spin without a floor."""
    cache_set_calls: list[tuple[str, str, int]] = []

    async def _record_set(key, value, ttl_seconds=None):
        cache_set_calls.append((key, value, ttl_seconds))

    async def _broken_incr(*_a, **_kw):
        raise RuntimeError("redis down")

    with patch.object(nss, "cache_incr", side_effect=_broken_incr), \
         patch.object(nss, "cache_set", side_effect=_record_set):
        count = await nss._record_provider_failure(
            "anthropic", lane="news",
        )
    assert count == 1
    # 1h floor when we don't know the real count.
    assert any(ttl == 3600 for (_k, _v, ttl) in cache_set_calls)


@pytest.mark.asyncio
async def test_clear_provider_cooldown_deletes_both_keys():
    """A successful batch must clear BOTH the cooldown marker and the
    fail-count counter — otherwise a flapping provider that briefly
    recovers carries stale fail-count forward and escalates to the
    6h cap on the next blip."""
    deleted: list[str] = []

    async def _record_delete(key):
        deleted.append(key)

    with patch.object(nss, "cache_delete", side_effect=_record_delete):
        await nss._clear_provider_cooldown("anthropic")

    assert "sentiment:cooldown:anthropic" in deleted
    assert "sentiment:fail_count:anthropic" in deleted


@pytest.mark.asyncio
async def test_score_pending_skips_when_provider_in_cooldown(
    db_session: AsyncSession,
):
    """Cooldown short-circuits before the cap reservation, so no LLM
    calls happen and no cap units are burned. Returns the new
    `cooldown_active=1` flag so the cron status surfaces it
    distinctly from `cap_hit`."""
    await insert_news_articles(db_session, [
        _row(f"news {i}", f"https://example.com/{i}", symbol=f"X{i}")
        for i in range(3)
    ])

    cap_calls = {"n": 0}
    stream_calls = {"n": 0}

    async def _count_cap(*_a, **_kw):
        cap_calls["n"] += 1
        return True

    async def _count_stream(*_a, **_kw):
        stream_calls["n"] += 1
        yield {"type": "delta", "text": "[]"}

    async def _in_cooldown(provider):
        return True

    with patch.object(nss, "_is_provider_in_cooldown", side_effect=_in_cooldown), \
         patch.object(nss, "_can_make_llm_call", side_effect=_count_cap), \
         patch.object(nss, "stream_chat", side_effect=_count_stream):
        result = await nss.score_pending(
            db=db_session, batch_size=10, max_batches=3,
            provider="anthropic", model="claude-haiku",
        )

    assert result["cooldown_active"] == 1
    assert result["batches"] == 0
    assert result["cap_hit"] == 0
    # No cap reserved, no LLM call. The whole point.
    assert cap_calls["n"] == 0
    assert stream_calls["n"] == 0


@pytest.mark.asyncio
async def test_score_pending_records_provider_failure_on_batch_error(
    db_session: AsyncSession,
):
    """When `_score_batch` returns `(None, error)` the cron path must
    engage the cooldown via `_record_provider_failure` — not just
    break the loop. The previous code only broke, leaving the next
    hour's run free to re-burn cap units on the same dead provider.
    """
    await insert_news_articles(db_session, [
        _row("news a", "https://example.com/a", symbol="2330"),
    ])

    record_calls: list[tuple[str, str]] = []

    async def _record(provider, *, lane):
        record_calls.append((provider, lane))

    async def _failing_batch(*_a, **_kw):
        return None, "HTTP 503 from upstream"

    async def _ok_cap(*_a, **_kw):
        return True

    async def _no_cooldown(*_a, **_kw):
        return False

    with patch.object(nss, "_is_provider_in_cooldown", side_effect=_no_cooldown), \
         patch.object(nss, "_can_make_llm_call", side_effect=_ok_cap), \
         patch.object(nss, "_score_batch", side_effect=_failing_batch), \
         patch.object(nss, "_record_provider_failure", side_effect=_record):
        result = await nss.score_pending(
            db=db_session, batch_size=10, max_batches=3,
            provider="anthropic", model="claude-haiku",
        )

    assert result["scored"] == 0
    assert ("anthropic", "news") in record_calls
    # Exactly one failure recorded — the loop breaks after the first
    # failure to avoid burning the rest of the cap on the same mode.
    assert len(record_calls) == 1


@pytest.mark.asyncio
async def test_score_pending_clears_cooldown_on_first_success(
    db_session: AsyncSession,
):
    """A successful batch must call `_clear_provider_cooldown` so a
    flapping provider doesn't carry stale failure state forward."""
    await insert_news_articles(db_session, [
        _row("news a", "https://example.com/a", symbol="2330"),
    ])
    from sqlalchemy import select
    from models.news_article import NewsArticle
    article = (await db_session.scalars(select(NewsArticle).limit(1))).first()
    assert article is not None

    clear_calls: list[str] = []

    async def _record_clear(provider):
        clear_calls.append(provider)

    async def _ok_batch(rows, *, provider, model, db):
        return [
            nss._Scored(article_id=rows[0].id, score=0.5, label="bullish"),
        ], None

    async def _ok_cap(*_a, **_kw):
        return True

    async def _no_cooldown(*_a, **_kw):
        return False

    with patch.object(nss, "_is_provider_in_cooldown", side_effect=_no_cooldown), \
         patch.object(nss, "_can_make_llm_call", side_effect=_ok_cap), \
         patch.object(nss, "_score_batch", side_effect=_ok_batch), \
         patch.object(nss, "_clear_provider_cooldown", side_effect=_record_clear):
        result = await nss.score_pending(
            db=db_session, batch_size=10, max_batches=3,
            provider="anthropic", model="claude-haiku",
        )

    assert result["scored"] >= 1
    assert clear_calls == ["anthropic"]
    assert article.id is not None
