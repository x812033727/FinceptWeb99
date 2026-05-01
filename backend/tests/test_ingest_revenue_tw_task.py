"""Tests for tasks.ingest_revenue_tw — daily monthly-revenue ingest."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_revenue_monthly import TwRevenueMonthly


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_revenue_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _row(symbol: str, date_str: str, *, revenue=10_000_000,
         yoy: float | None = 12.5, mom: float | None = 5.0) -> dict:
    return {
        "symbol":      symbol,
        "date":        date_str,
        "revenue":     revenue,
        "revenue_yoy": yoy,
        "revenue_mom": mom,
    }


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import ingest_revenue_tw

    with patch(
        "tasks.ingest_revenue_tw.acquire_lock",
        AsyncMock(return_value=False),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        AsyncMock(),
    ) as fm:
        await ingest_revenue_tw.run()

    fm.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_writes_rows(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_revenue_tw

    rows = [
        _row("2330", "2026-04-01", revenue=200_000_000, yoy=15.2, mom=3.1),
        _row("2454", "2026-04-01", revenue=80_000_000, yoy=22.0, mom=8.5),
    ]

    with patch(
        "tasks.ingest_revenue_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_revenue_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        AsyncMock(return_value=rows),
    ), patch(
        "tasks.ingest_revenue_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_revenue_tw.run()

    db_rows = (await db_session.scalars(
        select(TwRevenueMonthly).where(
            TwRevenueMonthly.symbol.in_(["2330", "2454"]),
        )
    )).all()
    assert len(db_rows) == 2
    by_sym = {r.symbol: r for r in db_rows}
    assert int(by_sym["2330"].revenue) == 200_000_000
    assert float(by_sym["2330"].revenue_yoy) == 15.2
    assert by_sym["2330"].source == "finmind"

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_handles_missing_growth_for_new_listing(
    patch_session, db_session: AsyncSession,
):
    """Newly-IPO'd companies have no prior-year baseline → FinMind
    returns empty / None for `revenue_year`. Coerce to NULL rather
    than dropping the row."""
    from tasks import ingest_revenue_tw

    rows = [
        _row("9999", "2026-04-01", yoy=None, mom=None),
    ]

    with patch(
        "tasks.ingest_revenue_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_revenue_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        AsyncMock(return_value=rows),
    ), patch(
        "tasks.ingest_revenue_tw.record_health", AsyncMock(),
    ):
        await ingest_revenue_tw.run()

    row = await db_session.scalar(
        select(TwRevenueMonthly).where(TwRevenueMonthly.symbol == "9999")
    )
    assert row is not None
    assert row.revenue_yoy is None
    assert row.revenue_mom is None


@pytest.mark.asyncio
async def test_rerun_overwrites(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_revenue_tw

    common = (
        patch(
            "tasks.ingest_revenue_tw.acquire_lock",
            AsyncMock(return_value=True),
        ),
        patch("tasks.ingest_revenue_tw.release_lock", AsyncMock()),
        patch(
            "tasks.ingest_revenue_tw.backoff_remaining_seconds",
            AsyncMock(return_value=0),
        ),
        patch("tasks.ingest_revenue_tw.clear_failures", AsyncMock()),
        patch("tasks.ingest_revenue_tw.record_health", AsyncMock()),
    )
    for ctx in common:
        ctx.__enter__()
    try:
        with patch(
            "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
            AsyncMock(return_value=[_row("2330", "2026-04-01", revenue=100, yoy=5)]),
        ):
            await ingest_revenue_tw.run()
        with patch(
            "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
            AsyncMock(return_value=[_row("2330", "2026-04-01", revenue=200, yoy=12)]),
        ):
            await ingest_revenue_tw.run()
    finally:
        for ctx in common:
            ctx.__exit__(None, None, None)

    db_rows = (await db_session.scalars(
        select(TwRevenueMonthly).where(TwRevenueMonthly.symbol == "2330")
    )).all()
    assert len(db_rows) == 1
    assert int(db_rows[0].revenue) == 200
    assert float(db_rows[0].revenue_yoy) == 12.0


@pytest.mark.asyncio
async def test_top_revenue_growers_aggregator(db_session: AsyncSession):
    """Repository aggregator returns highest YoY growers in the latest
    available month. Rows with NULL yoy are excluded."""
    from datetime import date as _date
    from services.ingest.repository import (
        RevenueMonthlyRow,
        read_top_revenue_growers,
        upsert_revenue_monthly,
    )

    ts = _date(2026, 4, 1)
    await upsert_revenue_monthly(db_session, [
        RevenueMonthlyRow(market="TW", symbol="2330", ts=ts,
                          revenue=200_000_000, revenue_yoy=15.2,
                          revenue_mom=3.0, source="finmind"),
        RevenueMonthlyRow(market="TW", symbol="2454", ts=ts,
                          revenue=80_000_000, revenue_yoy=42.5,
                          revenue_mom=12.0, source="finmind"),
        RevenueMonthlyRow(market="TW", symbol="9999", ts=ts,
                          revenue=10_000_000, revenue_yoy=None,
                          revenue_mom=None, source="finmind"),
    ])

    top = await read_top_revenue_growers(db_session, market="TW", limit=10)
    assert [r["symbol"] for r in top] == ["2454", "2330"]
    assert top[0]["revenue_yoy"] == 42.5


# ── paywall fail-soft path ────────────────────────────────────────

def _http_status_error(status_code: int, body_msg: str) -> Exception:
    """Build an `httpx.HTTPStatusError` whose `response.json()` returns
    `{"msg": body_msg}`. Mirrors what FinMind actually emits when the
    request token is on a tier that doesn't have access to the
    requested dataset."""
    import httpx
    request = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    response = httpx.Response(
        status_code,
        request=request,
        json={"msg": body_msg, "status": status_code},
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


@pytest.mark.asyncio
async def test_paywall_response_is_marked_skipped_not_failure(patch_session):
    """As of 2026-04 FinMind moved the market-wide
    `TaiwanStockMonthRevenue` query to paid-sponsor-only. The
    response is HTTP 400 with body `{"msg": "Your level is register.
    Please update your user level. ..."}`. Treat it as a known-permanent
    skip: clear failures (no nag-the-admin auto-backoff banner),
    don't bump the failure counter, and write a `skipped: ...` health
    record that quotes the upstream message verbatim so an operator
    instantly recognises FinMind's wording."""
    from tasks import ingest_revenue_tw

    body_msg = (
        "Your level is register. Please update your user level. "
        "Detail information: https://finmindtrade.com/analysis/#/Sponsor/sponsor"
    )
    record_health_mock = AsyncMock()
    record_failure_mock = AsyncMock()
    clear_failures_mock = AsyncMock()
    with patch(
        "tasks.ingest_revenue_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_revenue_tw.record_failure", record_failure_mock,
    ), patch(
        "tasks.ingest_revenue_tw.clear_failures", clear_failures_mock,
    ), patch(
        "tasks.ingest_revenue_tw.record_health", record_health_mock,
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        AsyncMock(side_effect=_http_status_error(400, body_msg)),
    ):
        await ingest_revenue_tw.run()

    # Did NOT bump failure counter — paywall is permanent state, not transient.
    record_failure_mock.assert_not_called()
    # DID clear failures — even if a previous tick had armed backoff,
    # the paywall path resets it so the admin's "Retry now" stops
    # surfacing a stale "auto-backoff armed" banner.
    clear_failures_mock.assert_awaited_once()
    # Health row says skipped + quotes upstream verbatim.
    record_health_mock.assert_awaited_once()
    kwargs = record_health_mock.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert "skipped" in kwargs["error"].lower()
    assert "paywalled" in kwargs["error"].lower()
    assert "your level is register" in kwargs["error"].lower()
    # No "auto-backoff armed" wording — that's reserved for transient
    # outages; admins shouldn't be misled into thinking this will
    # self-recover on retry.
    assert "auto-backoff armed" not in kwargs["error"]


@pytest.mark.asyncio
async def test_genuine_outage_still_arms_backoff(patch_session):
    """Sanity check: a 503 / generic transient failure still uses the
    record_failure + auto-backoff path. Only the paywall message
    diverts to skip-mode."""
    from tasks import ingest_revenue_tw

    record_health_mock = AsyncMock()
    record_failure_mock = AsyncMock(return_value=1)
    clear_failures_mock = AsyncMock()
    with patch(
        "tasks.ingest_revenue_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_revenue_tw.record_failure", record_failure_mock,
    ), patch(
        "tasks.ingest_revenue_tw.clear_failures", clear_failures_mock,
    ), patch(
        "tasks.ingest_revenue_tw.record_health", record_health_mock,
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        AsyncMock(side_effect=_http_status_error(503, "FinMind unavailable")),
    ):
        await ingest_revenue_tw.run()

    record_failure_mock.assert_awaited_once()
    clear_failures_mock.assert_not_called()
    kwargs = record_health_mock.await_args.kwargs
    assert kwargs["ok"] is False
    assert "auto-backoff armed" in kwargs["error"]
    assert "skipped" not in kwargs["error"].lower()


# ── _looks_like_paywall unit ──────────────────────────────────────

def test_paywall_detector_is_case_insensitive():
    from tasks.ingest_revenue_tw import _looks_like_paywall
    assert _looks_like_paywall("YOUR LEVEL IS REGISTER")
    assert _looks_like_paywall("Your Level is Register. Please update")
    assert _looks_like_paywall("please update your user level")


def test_paywall_detector_matches_sponsor_phrasing():
    from tasks.ingest_revenue_tw import _looks_like_paywall
    assert _looks_like_paywall("This dataset requires paid sponsor")
    assert _looks_like_paywall("Sponsor only")


def test_paywall_detector_does_not_match_generic_400():
    from tasks.ingest_revenue_tw import _looks_like_paywall
    assert not _looks_like_paywall("")
    assert not _looks_like_paywall("Bad request")
    assert not _looks_like_paywall("invalid date format")
    assert not _looks_like_paywall("token expired")
