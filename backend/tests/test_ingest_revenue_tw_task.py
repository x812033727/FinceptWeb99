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


def _row(symbol: str, date_str: str, *, revenue=10_000_000) -> dict:
    """FinMind row shape after PR #211 — connector no longer fakes
    yoy/mom from period year/month integers. Growth rates are computed
    by the ingest task itself (`_enrich_growth_rates`)."""
    return {
        "symbol":  symbol,
        "date":    date_str,
        "revenue": revenue,
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
        _row("2330", "2026-04-01", revenue=200_000_000),
        _row("2454", "2026-04-01", revenue=80_000_000),
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
    assert by_sym["2330"].source == "finmind"
    # No prev-year-same-month or prev-month rows in DB → growth rates
    # are None. The dedicated `test_enrich_growth_rates_*` tests below
    # cover the populated-history path.
    assert by_sym["2330"].revenue_yoy is None
    assert by_sym["2330"].revenue_mom is None

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_handles_missing_growth_for_new_listing(
    patch_session, db_session: AsyncSession,
):
    """Newly-IPO'd companies have no prior-year baseline in
    `tw_revenue_monthly` → `_enrich_growth_rates` returns None for
    both yoy and mom rather than crashing on a None lookup."""
    from tasks import ingest_revenue_tw

    rows = [
        _row("9999", "2026-04-01"),
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
            AsyncMock(return_value=[_row("2330", "2026-04-01", revenue=100)]),
        ):
            await ingest_revenue_tw.run()
        with patch(
            "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
            AsyncMock(return_value=[_row("2330", "2026-04-01", revenue=200)]),
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


# ── Growth-rate computation (PR #211) ────────────────────────────


def test_shift_month_handles_year_rollover():
    """`_shift_month` is the YoY/MoM date arithmetic helper. Must
    correctly walk across year boundaries (Feb 2026 - 1 = Jan 2026,
    Feb 2026 - 12 = Feb 2025, Jan 2026 - 1 = Dec 2025)."""
    from datetime import date as _date

    from tasks.ingest_revenue_tw import _shift_month
    assert _shift_month(_date(2026, 2, 1), -1) == _date(2026, 1, 1)
    assert _shift_month(_date(2026, 2, 1), -12) == _date(2025, 2, 1)
    assert _shift_month(_date(2026, 1, 1), -1) == _date(2025, 12, 1)
    assert _shift_month(_date(2026, 1, 1), -12) == _date(2025, 1, 1)


def test_pct_change_handles_zero_and_none_baseline():
    """Division-by-zero and missing-baseline must both yield None
    rather than raising — newly-IPO'd companies have no prev-year row,
    and shell companies sometimes file zero revenue."""
    from tasks.ingest_revenue_tw import _pct_change
    assert _pct_change(110, 100) == 10.0
    assert _pct_change(80, 100) == -20.0
    assert _pct_change(100, None) is None
    assert _pct_change(None, 100) is None
    assert _pct_change(100, 0) is None


def test_pct_change_abstains_when_the_ratio_cannot_be_stored():
    """`revenue_yoy` / `revenue_mom` are Numeric(8, 2) — anything past
    999999.99 makes asyncpg raise NumericValueOutOfRange and takes the
    whole bulk upsert down with it. A baseline that small (a dormant
    month, a shell company) makes the percentage noise anyway, so
    abstain exactly like a zero baseline does.
    """
    from tasks.ingest_revenue_tw import _pct_change

    # 1 thousand NTD a year ago, 10 billion today — real shape in the
    # TW universe, and ~1,000,000,000% once divided out.
    assert _pct_change(10_000_000_000, 1_000) is None
    assert _pct_change(-10_000_000_000, 1_000) is None

    # The boundary itself still stores.
    assert _pct_change(1_000_000, 100) == 999_900.0
    assert _pct_change(999, 100) == 899.0


@pytest.mark.asyncio
async def test_enrich_growth_rates_computes_yoy_from_db_history(
    db_session: AsyncSession,
):
    """Pre-seed DB with the prev-year-same-month row for 2330. Run
    the ingest with a mocked FinMind row for the current month. The
    upsert must land with a correctly computed `revenue_yoy` derived
    from comparing current revenue to the seeded historical row."""
    from datetime import date as _date

    from services.ingest.repository import (
        RevenueMonthlyRow,
        upsert_revenue_monthly,
    )
    from tasks import ingest_revenue_tw

    # Historical baseline: Apr 2025 had 100M revenue.
    await upsert_revenue_monthly(db_session, [
        RevenueMonthlyRow(
            market="TW", symbol="2330", ts=_date(2025, 4, 1),
            revenue=100_000_000, revenue_yoy=None, revenue_mom=None,
            source="finmind",
        ),
    ])

    # Mock FinMind to return Apr 2026 with 130M (= +30% YoY).
    rows = [_row("2330", "2026-04-01", revenue=130_000_000)]

    with patch(
        "tasks.ingest_revenue_tw.AsyncSessionLocal",
        return_value=_PassthroughCM(db_session),
    ), patch(
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
        select(TwRevenueMonthly).where(
            TwRevenueMonthly.symbol == "2330",
            TwRevenueMonthly.ts == _date(2026, 4, 1),
        )
    )
    assert row is not None
    assert int(row.revenue) == 130_000_000
    # (130M - 100M) / 100M * 100 = 30.0
    assert float(row.revenue_yoy) == 30.0


@pytest.mark.asyncio
async def test_enrich_growth_rates_computes_mom_from_in_payload_history(
    db_session: AsyncSession,
):
    """When the FinMind response itself contains the prev-month row
    (which happens because we pull 90 days of lookback), MoM is
    computed from the in-memory payload — no DB round-trip needed."""
    rows = [
        _row("2330", "2026-03-01", revenue=100_000_000),
        _row("2330", "2026-04-01", revenue=110_000_000),  # +10% MoM
    ]

    with patch(
        "tasks.ingest_revenue_tw.AsyncSessionLocal",
        return_value=_PassthroughCM(db_session),
    ), patch(
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
        from tasks import ingest_revenue_tw
        await ingest_revenue_tw.run()

    from datetime import date as _date
    apr = await db_session.scalar(
        select(TwRevenueMonthly).where(
            TwRevenueMonthly.symbol == "2330",
            TwRevenueMonthly.ts == _date(2026, 4, 1),
        )
    )
    assert apr is not None
    assert float(apr.revenue_mom) == 10.0


class _PassthroughCM:
    """Reusable async-context-manager fixture that yields a shared
    `db_session` so the ingest task's `AsyncSessionLocal()` writes
    land in the test's transaction. Same pattern as the
    `patch_session` fixture but reusable inline so the new tests can
    layer additional patches on top."""
    def __init__(self, db_session):
        self._db = db_session

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


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
async def test_silent_deny_records_skipped_with_silent_deny_field(patch_session):
    """FinMind sometimes returns HTTP 200 with body.status != 200
    (silent paywall / tier-unavailable). The connector swallows it
    into `[]` so live-serving paths fall back gracefully — but the
    ingest task must surface this as ok=False so the admin badge
    shows the rejection instead of a fake-green ok=True row_count=0.

    Tests the full silent-deny pipeline:
      - `consume_last_silent_deny()` is read after the FinMind call
      - `raise_if_silent_denied` translates it into FinMindSilentDeny
      - the task's `except FinMindSilentDeny` branch records ok=False
        with `silent_deny=...` + `source="finmind"` so Prometheus
        counters and the admin UI can distinguish silent paywall from
        a real outage."""
    from tasks import ingest_revenue_tw

    body_msg = "Your level is register, not sponsor."

    async def _silent_deny_call(*_a, **_k):
        # Mirrors what `_query` does on a silent-deny response: sets
        # the contextvar then returns []. The task layer's
        # `raise_if_silent_denied` reads the contextvar and raises.
        from data.tw.finmind_connector import _last_silent_deny
        _last_silent_deny.set(body_msg)
        return []

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
        AsyncMock(side_effect=_silent_deny_call),
    ):
        await ingest_revenue_tw.run()

    # Permanent state — don't arm backoff, don't bump failure counter.
    record_failure_mock.assert_not_called()
    clear_failures_mock.assert_awaited_once()
    record_health_mock.assert_awaited_once()
    kwargs = record_health_mock.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert kwargs["silent_deny"] == body_msg
    assert kwargs["source"] == "finmind"
    assert "silent_paywall" in kwargs["error"]
    assert body_msg in kwargs["error"]


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


# Detector-level unit tests (`_looks_like_paywall`,
# `_extract_body_message`, `is_paywall_response`) live in
# `test_finmind_paywall.py` since PR #188 — the helpers moved to
# `data.tw.finmind_paywall` so other FinMind cron tasks can adopt
# the same fail-soft pattern without re-implementing.


@pytest.mark.asyncio
async def test_queries_month_first_dates_not_a_lookback_offset(
    patch_session, db_session: AsyncSession,
):
    """FinMind's market-wide mode (`data_id=""`) returns rows for the
    single `start_date` and ignores `end_date`, and revenue rows only
    ever land on the first of a month. Passing `today - 90d` therefore
    matched nothing on ~29 days out of 30 — the job has to ask for each
    month-first in the window explicitly.
    """
    from tasks import ingest_revenue_tw

    seen: list[str] = []

    async def _fake_market_wide(start_date, end_date=None):
        seen.append(start_date)
        return [_row("2330", start_date, revenue=100_000_000)]

    with patch(
        "tasks.ingest_revenue_tw.acquire_lock", AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_revenue_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        _fake_market_wide,
    ), patch(
        "tasks.ingest_revenue_tw.record_health", AsyncMock(),
    ):
        await ingest_revenue_tw.run()

    assert seen, "job made no upstream call"
    assert all(d.endswith("-01") for d in seen), (
        f"every market-wide query must target a month-first, got {seen}"
    )
    assert len(seen) == len(set(seen)), f"duplicate queries: {seen}"
    # 90-day window spans 3-4 month-firsts depending on today's date.
    assert 3 <= len(seen) <= 4, f"unexpected window: {seen}"


@pytest.mark.asyncio
async def test_empty_upstream_across_all_months_is_recorded_not_swallowed(
    patch_session, db_session: AsyncSession,
):
    """Zero rows from every month-first means the upstream gave us
    nothing — that must reach the health row as row_count=0 rather than
    looking like a healthy write."""
    from tasks import ingest_revenue_tw

    with patch(
        "tasks.ingest_revenue_tw.acquire_lock", AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_revenue_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_revenue_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_revenue_tw.finmind.get_monthly_revenue_market_wide",
        AsyncMock(return_value=[]),
    ), patch(
        "tasks.ingest_revenue_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_revenue_tw.run()

    assert health.await_args.kwargs["row_count"] == 0
