"""Tests for services.news_backfill_service.

Coverage:
  - Archive populated → skip backfill (one COUNT, no FinMind call)
  - Archive empty → trigger FinMind, insert rows, return covered=True
  - FinMind returns nothing (paywall / no news) → silent failure,
    covered=False, no exception leaks
  - Concurrent rounds (lock contention) → second caller skips
  - Non-TW market → skip entirely (FinMind dataset is TW-specific)
"""
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services import news_backfill_service
from services.ingest.repository import (
    NewsArticleRow,
    insert_news_articles,
)


def _seed_articles(market: str, n: int, base_dt: datetime) -> list[NewsArticleRow]:
    """Build N distinct news rows clustered around `base_dt` so the
    coverage-window probe sees them."""
    return [
        NewsArticleRow(
            market=market,
            symbol=None,
            published_at=base_dt - timedelta(days=i),
            title=f"seed news {i}",
            link=f"https://example.com/seed/{i}",
            publisher="Test Publisher",
            summary=None,
            payload=None,
            source="test_seed",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_skips_backfill_when_archive_already_covered(
    db_session: AsyncSession,
):
    """Hot path: when there are >= _MIN_ARTICLES_THRESHOLD rows in the
    14-day window, return immediately without calling FinMind."""
    as_of = date(2026, 3, 23)
    base_dt = datetime(2026, 3, 22, 12, 0, tzinfo=UTC)
    await insert_news_articles(
        db_session, _seed_articles("TW", 10, base_dt),
    )

    finmind_mock = AsyncMock()
    with patch.object(
        news_backfill_service, "_do_backfill", new=finmind_mock,
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is True
    assert out["backfilled"] == 0
    finmind_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_triggers_backfill_when_archive_empty(
    db_session: AsyncSession,
):
    """Cold path: with no articles in the window, FinMind backfill
    fires. Verified via mocked `_do_backfill` so the test doesn't need
    real FinMind. Returns `backfilled` count from the helper."""
    as_of = date(2024, 6, 15)  # well before any test seed data

    async def _fake_backfill(*, market, as_of):
        # Simulate FinMind returning rows that get inserted.
        await insert_news_articles(
            db_session,
            _seed_articles("TW", 8, datetime(2024, 6, 10, 9, 0, tzinfo=UTC)),
        )
        return 8

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=_fake_backfill),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["backfilled"] == 8
    assert out["covered"] is True


@pytest.mark.asyncio
async def test_silent_fail_when_finmind_returns_nothing(
    db_session: AsyncSession,
):
    """No paid token / paywalled response / FinMind empty for the
    window: backfill yields zero rows. The helper must return
    `covered=False` without raising — the discussion still runs and
    surfaces the existing empty-archive warning."""
    as_of = date(2024, 6, 15)

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(return_value=0),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["backfilled"] == 0
    assert out["covered"] is False
    assert "error" not in out  # silent — not treated as an error


@pytest.mark.asyncio
async def test_silent_fail_when_finmind_raises(
    db_session: AsyncSession,
):
    """A FinMind 5xx during backfill must not abort the round. Helper
    catches the exception, logs, returns `covered=False, error=...`
    so the caller can record it without re-raising."""
    as_of = date(2024, 6, 15)

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=RuntimeError("FinMind 502")),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is False
    assert "FinMind 502" in out.get("error", "")


@pytest.mark.asyncio
async def test_paywall_body_message_surfaced_in_error(
    db_session: AsyncSession,
):
    """When FinMind responds with HTTP 400 + a paywall body
    ("Your level is register. Please update your user level."),
    the error string in `ctx['news_backfill']` must call out the tier
    mismatch explicitly. Without this users see a generic httpx
    "400 Bad Request" and have no clue why their paid token isn't
    working — could be wrong tier, expired, etc."""
    import httpx

    as_of = date(2024, 6, 15)

    # Build a real httpx.HTTPStatusError with the paywall body
    # FinMind actually returns. The test flows through the same
    # `extract_body_message` path the production code uses.
    fake_request = httpx.Request("GET", "https://api.finmindtrade.com/")
    fake_response = httpx.Response(
        400,
        request=fake_request,
        json={
            "msg": "Your level is register. Please update your user level. "
                   "Detail information: https://finmindtrade.com/...",
            "status": 400,
        },
    )
    paywall_exc = httpx.HTTPStatusError(
        "400 Bad Request", request=fake_request, response=fake_response,
    )

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=paywall_exc),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is False
    assert out["backfilled"] == 0
    assert "paywall" in out["error"].lower()
    assert "Your level is register" in out["error"]


@pytest.mark.asyncio
async def test_generic_finmind_body_message_surfaced_in_error(
    db_session: AsyncSession,
):
    """Non-paywall FinMind 400 (bad params, dataset-not-found, etc.):
    surface the upstream `msg` rather than the generic httpx error
    string. Helps the user distinguish "wrong tier" from "wrong API
    usage"."""
    import httpx

    as_of = date(2024, 6, 15)
    fake_request = httpx.Request("GET", "https://api.finmindtrade.com/")
    fake_response = httpx.Response(
        400,
        request=fake_request,
        json={"msg": "Invalid date range", "status": 400},
    )
    bad_request_exc = httpx.HTTPStatusError(
        "400", request=fake_request, response=fake_response,
    )

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=bad_request_exc),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["error"] == "FinMind error: Invalid date range"
    assert "paywall" not in out["error"].lower()


@pytest.mark.asyncio
async def test_no_body_falls_back_to_sanitised_httpx_string(
    db_session: AsyncSession,
):
    """When the response body isn't parseable as JSON (network failure
    mid-response, FinMind returns HTML), fall back to httpx's default
    string but redact any token query param so it doesn't leak into
    the error visible to the user / logs."""
    as_of = date(2024, 6, 15)

    # Plain RuntimeError without an exc.response — extract_body_message
    # returns "" → fall through to sanitised str(exc).
    exc = RuntimeError(
        "Client error '400 Bad Request' for url "
        "'https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockNews"
        "&token=eyJsuperSecretJWT.payload.signature&end_date=2024-07-15'"
    )

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=exc),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert "<redacted>" in out["error"]
    assert "eyJsuperSecretJWT" not in out["error"]
    # URL structure preserved for diagnosis
    assert "dataset=TaiwanStockNews" in out["error"]


def test_sanitise_token_strips_jwt_value():
    """Standalone test for the redaction helper. Must:
      - replace the token VALUE with `<redacted>`
      - preserve the rest of the URL for diagnostics
      - handle multiple separators (& space ' ")
      - leave strings without tokens untouched"""
    s = ("https://api.finmindtrade.com/api/v4/data?dataset=X"
         "&token=eyJ.abc.def&start_date=2024-01-01")
    out = news_backfill_service._sanitise_token(s)
    assert "eyJ.abc.def" not in out
    assert "token=<redacted>" in out
    assert "dataset=X" in out
    assert "start_date=2024-01-01" in out

    # No-token input passes through unchanged.
    assert news_backfill_service._sanitise_token(
        "no secrets here",
    ) == "no secrets here"


# ── PR #218: per-symbol fan-out fallback on paywall ──────────────


@pytest.mark.asyncio
async def test_paywall_with_focus_symbols_falls_back_to_per_symbol(
    db_session: AsyncSession,
):
    """When market-wide hits a Sponsor-tier paywall AND the discussion
    has `focus_symbols`, the helper falls back to per-symbol fan-out
    rather than just surrendering. This is the Sponsor-friendly path:
    the user keeps news for the tickers they actually care about
    even though `data_id=""` is blocked."""
    import httpx

    as_of = date(2024, 6, 15)
    fake_request = httpx.Request("GET", "https://api.finmindtrade.com/")
    fake_response = httpx.Response(
        400, request=fake_request,
        json={"msg": "Your level is register. Please update your user level.",
              "status": 400},
    )
    paywall_exc = httpx.HTTPStatusError(
        "400", request=fake_request, response=fake_response,
    )

    per_symbol_mock = AsyncMock(return_value=4)

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=paywall_exc),
    ), patch.object(
        news_backfill_service, "_do_backfill_per_symbol",
        new=per_symbol_mock,
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
            focus_symbols=["2330", "2454"],
        )

    # Per-symbol fallback fired with the focus symbols (capped, but
    # 2 < cap so passes through unchanged).
    per_symbol_mock.assert_awaited_once()
    assert per_symbol_mock.call_args.kwargs["symbols"] == ["2330", "2454"]
    assert out["fallback"] == "per-symbol"
    assert out["backfilled"] == 4
    assert "error" not in out


@pytest.mark.asyncio
async def test_paywall_caps_focus_symbols_at_per_symbol_cap(
    db_session: AsyncSession,
):
    """A discussion topic mentioning many tickers (rare but possible
    — `extract_focus_symbols` returns up to ~5 typically) shouldn't
    fan out across all of them when paywall fires. Cap at
    `_PER_SYMBOL_CAP=5` to bound auto-backfill latency + quota."""
    import httpx

    as_of = date(2024, 6, 15)
    fake_request = httpx.Request("GET", "https://api.finmindtrade.com/")
    fake_response = httpx.Response(
        400, request=fake_request,
        json={"msg": "Sponsor tier required", "status": 400},
    )
    paywall_exc = httpx.HTTPStatusError(
        "400", request=fake_request, response=fake_response,
    )

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=paywall_exc),
    ), patch.object(
        news_backfill_service, "_do_backfill_per_symbol",
        new=AsyncMock(return_value=2),
    ) as ps_mock:
        await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
            focus_symbols=["2330", "2454", "2317", "2308", "1101", "2412", "3008"],
        )

    # Capped at _PER_SYMBOL_CAP (=5), even though caller provided 7.
    passed_symbols = ps_mock.call_args.kwargs["symbols"]
    assert len(passed_symbols) == news_backfill_service._PER_SYMBOL_CAP
    assert passed_symbols == ["2330", "2454", "2317", "2308", "1101"]


@pytest.mark.asyncio
async def test_paywall_without_focus_symbols_returns_paywall_error(
    db_session: AsyncSession,
):
    """Paywall + no focus symbols → no fallback (no good default
    universe to fan out across). Returns the paywall error so the
    user knows to either pass focus symbols or upgrade tier."""
    import httpx

    as_of = date(2024, 6, 15)
    fake_request = httpx.Request("GET", "https://api.finmindtrade.com/")
    fake_response = httpx.Response(
        400, request=fake_request,
        json={"msg": "Your level is register. Please update.", "status": 400},
    )
    paywall_exc = httpx.HTTPStatusError(
        "400", request=fake_request, response=fake_response,
    )

    per_symbol_mock = AsyncMock()

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=paywall_exc),
    ), patch.object(
        news_backfill_service, "_do_backfill_per_symbol",
        new=per_symbol_mock,
    ):
        # No focus_symbols → no fallback path.
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of, focus_symbols=None,
        )

    per_symbol_mock.assert_not_awaited()
    assert "paywall" in out["error"].lower()
    assert "fallback" not in out


@pytest.mark.asyncio
async def test_per_symbol_fallback_failure_surfaces_error(
    db_session: AsyncSession,
):
    """If even the per-symbol path fails (e.g. Sponsor tier ALSO
    can't query news for some reason), the error from THAT call
    surfaces. Caller sees an actionable message rather than the
    original paywall (which would mislead since per-symbol was
    actually attempted)."""
    import httpx

    as_of = date(2024, 6, 15)
    fake_request = httpx.Request("GET", "https://api.finmindtrade.com/")
    fake_response = httpx.Response(
        400, request=fake_request,
        json={"msg": "Sponsor tier required", "status": 400},
    )
    paywall_exc = httpx.HTTPStatusError(
        "400", request=fake_request, response=fake_response,
    )
    per_symbol_exc = RuntimeError("FinMind 502")

    with patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(side_effect=paywall_exc),
    ), patch.object(
        news_backfill_service, "_do_backfill_per_symbol",
        new=AsyncMock(side_effect=per_symbol_exc),
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
            focus_symbols=["2330"],
        )

    assert "FinMind 502" in out["error"]
    assert "fallback" not in out
    assert out["backfilled"] == 0


@pytest.mark.asyncio
async def test_skips_non_tw_market(db_session: AsyncSession):
    """FinMind's TaiwanStockNews is TW-specific. Calling for a US
    discussion must skip immediately so the helper doesn't waste
    quota on a request that can never return useful data."""
    finmind_mock = AsyncMock()
    with patch.object(
        news_backfill_service, "_do_backfill", new=finmind_mock,
    ):
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="US", as_of=date(2024, 6, 15),
        )

    assert out["covered"] is False
    assert out["backfilled"] == 0
    assert out.get("skipped") == "non-tw"
    finmind_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_lock_contention_skips_second_caller(
    db_session: AsyncSession,
):
    """Two rounds for the same date hitting the helper concurrently:
    the Redis lock dedups, second caller bails out with skipped="lock".
    The first caller's backfill eventually populates the archive for
    everyone."""
    as_of = date(2024, 6, 15)

    # First caller: takes the lock (mocked to succeed, but never
    # releases for the duration of test). Second caller should see
    # acquire_lock fail.
    with patch.object(
        news_backfill_service, "acquire_lock",
        new=AsyncMock(return_value=False),
    ), patch.object(
        news_backfill_service, "_do_backfill",
        new=AsyncMock(return_value=0),
    ) as backfill_mock:
        out = await news_backfill_service.ensure_news_archive_covers(
            db_session, market="TW", as_of=as_of,
        )

    assert out["covered"] is False
    assert out.get("skipped") == "lock"
    backfill_mock.assert_not_awaited()
