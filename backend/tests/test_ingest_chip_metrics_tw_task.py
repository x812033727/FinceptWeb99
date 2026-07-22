"""Tests for tasks.ingest_institutional_tw and tasks.ingest_margin_tw.

Both pull the full TW market in one TWSE call per day and bulk-upsert
into `tw_institutional_daily` / `tw_margin_daily`. Tests exercise the
common patterns (lock skip, success path, idempotent re-run, backoff
preservation) for both jobs without duplicating fixtures.
"""
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_chip_metrics import TwInstitutionalDaily, TwMarginDaily


# ── shared fixtures ────────────────────────────────────────────────


@pytest.fixture
def patch_institutional_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_institutional_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


@pytest.fixture
def patch_margin_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_margin_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _institutional_row(symbol: str = "2330") -> dict:
    return {
        "symbol":      symbol,
        "name_zh":     f"name_{symbol}",
        "fini_buy":    10_000,
        "fini_sell":   2_000,
        "sitc_buy":    1_000,
        "sitc_sell":   500,
        "dealer_buy":  300,
        "dealer_sell": 100,
    }


def _margin_row(symbol: str = "2330") -> dict:
    return {
        "symbol":          symbol,
        "name_zh":         f"name_{symbol}",
        "margin_purchase": 5_000,
        "margin_balance":  20_000,
        "short_sale":      1_000,
        "short_balance":   3_000,
    }


# ── institutional ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_institutional_lock_held_skips_work(patch_institutional_session):
    from tasks import ingest_institutional_tw

    with patch(
        "tasks.ingest_institutional_tw.acquire_lock",
        AsyncMock(return_value=False),
    ), patch(
        "tasks.ingest_institutional_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.twse.get_institutional", AsyncMock(),
    ) as twse_call:
        await ingest_institutional_tw.run()

    twse_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_institutional_success_writes_rows(
    patch_institutional_session, db_session: AsyncSession,
):
    from tasks import ingest_institutional_tw

    rows = [_institutional_row("2330"), _institutional_row("2454")]

    # The job walks a window of unarchived weekdays, so answer only for
    # today and give every other date an empty table — the shape TWSE
    # returns for a non-trading day.
    async def _only_today(query_date=None, **_):
        return rows if query_date == date.today() else []

    with patch(
        "tasks.ingest_institutional_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_institutional_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_institutional_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.asyncio.sleep", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(side_effect=_only_today),
    ), patch(
        "tasks.ingest_institutional_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_institutional_tw.run()

    db_rows = (await db_session.scalars(
        select(TwInstitutionalDaily).where(
            TwInstitutionalDaily.symbol.in_(["2330", "2454"]),
        )
    )).all()
    assert len(db_rows) == 2
    by_sym = {r.symbol: r for r in db_rows}
    assert by_sym["2330"].fini_buy == 10_000
    assert by_sym["2330"].source == "twse"
    assert by_sym["2330"].ts == date.today()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_institutional_rerun_skips_already_archived_days(
    patch_institutional_session, db_session: AsyncSession,
):
    """A second run does not re-ask for a session already in the table.

    This is the whole point of the date walk: the steady state is one
    request for today, and only genuinely missing sessions are re-asked
    (which is how a timed-out day gets healed instead of lost forever).
    """
    from tasks import ingest_institutional_tw

    rows = [_institutional_row("2330")]

    async def _today_only(query_date=None, **_):
        return rows if query_date == date.today() else []

    common = (
        patch(
            "tasks.ingest_institutional_tw.acquire_lock",
            AsyncMock(return_value=True),
        ),
        patch("tasks.ingest_institutional_tw.release_lock", AsyncMock()),
        patch(
            "tasks.ingest_institutional_tw.backoff_remaining_seconds",
            AsyncMock(return_value=0),
        ),
        patch("tasks.ingest_institutional_tw.clear_failures", AsyncMock()),
        patch("tasks.ingest_institutional_tw.record_health", AsyncMock()),
        patch("tasks.ingest_institutional_tw.asyncio.sleep", AsyncMock()),
    )
    for ctx in common:
        ctx.__enter__()
    try:
        with patch(
            "tasks.ingest_institutional_tw.twse.get_institutional",
            AsyncMock(side_effect=_today_only),
        ):
            await ingest_institutional_tw.run()
        with patch(
            "tasks.ingest_institutional_tw.twse.get_institutional",
            AsyncMock(side_effect=_today_only),
        ) as second:
            await ingest_institutional_tw.run()
    finally:
        for ctx in common:
            ctx.__exit__(None, None, None)

    asked = {c.args[0] if c.args else c.kwargs.get("query_date")
             for c in second.await_args_list}
    assert date.today() not in asked

    db_rows = (await db_session.scalars(
        select(TwInstitutionalDaily).where(
            TwInstitutionalDaily.symbol == "2330",
        )
    )).all()
    assert len(db_rows) == 1


@pytest.mark.asyncio
async def test_institutional_same_day_write_is_an_upsert(
    patch_institutional_session, db_session: AsyncSession,
):
    """Writing the same session twice overwrites rather than duplicating
    — the property the date walk relies on when it re-fills a gap."""
    from tasks import ingest_institutional_tw

    with patch(
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(return_value=[_institutional_row("2330")]),
    ):
        await ingest_institutional_tw._ingest_day(date.today())
    with patch(
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(return_value=[{**_institutional_row("2330"), "fini_buy": 50_000}]),
    ):
        await ingest_institutional_tw._ingest_day(date.today())

    db_rows = (await db_session.scalars(
        select(TwInstitutionalDaily).where(
            TwInstitutionalDaily.symbol == "2330",
        )
    )).all()
    assert len(db_rows) == 1
    assert db_rows[0].fini_buy == 50_000


@pytest.mark.asyncio
async def test_institutional_partial_window_keeps_what_landed(
    patch_institutional_session, db_session: AsyncSession,
):
    """One day failing must not discard the days that succeeded, and the
    run still reports healthy — the gap is retried on the next tick."""
    from tasks import ingest_institutional_tw

    today = date.today()
    failing_day = today - timedelta(days=1)

    async def _one_bad_day(query_date=None, **_):
        if query_date == failing_day:
            raise httpx.ConnectTimeout("boom")
        return [_institutional_row("2330")] if query_date == today else []

    with patch(
        "tasks.ingest_institutional_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_institutional_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_institutional_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.asyncio.sleep", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(side_effect=_one_bad_day),
    ), patch(
        "tasks.ingest_institutional_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_institutional_tw.run()

    assert health.await_args.kwargs["ok"] is True
    db_rows = (await db_session.scalars(
        select(TwInstitutionalDaily).where(
            TwInstitutionalDaily.symbol == "2330",
        )
    )).all()
    assert len(db_rows) == 1


@pytest.mark.asyncio
async def test_institutional_all_days_failing_is_reported_unhealthy(
    patch_institutional_session,
):
    """A total outage must not read as a healthy zero-row run — that is
    the silent-failure mode this repo has been burned by before."""
    from tasks import ingest_institutional_tw

    with patch(
        "tasks.ingest_institutional_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_institutional_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_institutional_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.record_failure", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.get_failure_count",
        AsyncMock(return_value=1),
    ), patch(
        "tasks.ingest_institutional_tw.asyncio.sleep", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(side_effect=httpx.ConnectTimeout("down")),
    ), patch(
        "tasks.ingest_institutional_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_institutional_tw.run()

    assert health.await_args.kwargs["ok"] is False


@pytest.mark.asyncio
async def test_institutional_upstream_failure_records_unhealthy(
    patch_institutional_session,
):
    from tasks import ingest_institutional_tw

    with patch(
        "tasks.ingest_institutional_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_institutional_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_institutional_tw.record_failure",
        AsyncMock(return_value=1),
    ) as failures, patch(
        "tasks.ingest_institutional_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(side_effect=RuntimeError("twse 503")),
    ), patch(
        "tasks.ingest_institutional_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_institutional_tw.run()

    failures.assert_awaited_once()
    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is False
    assert "twse 503" in kwargs["error"]
    assert "failure #1" in kwargs["error"]


# ── margin ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_margin_success_writes_rows(
    patch_margin_session, db_session: AsyncSession,
):
    from tasks import ingest_margin_tw

    rows = [_margin_row("2330"), _margin_row("2454")]

    with patch(
        "tasks.ingest_margin_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_margin_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_margin_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_margin_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_margin_tw.twse.get_margin",
        AsyncMock(return_value=rows),
    ), patch(
        "tasks.ingest_margin_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_margin_tw.run()

    db_rows = (await db_session.scalars(
        select(TwMarginDaily).where(
            TwMarginDaily.symbol.in_(["2330", "2454"]),
        )
    )).all()
    assert len(db_rows) == 2
    by_sym = {r.symbol: r for r in db_rows}
    assert by_sym["2330"].margin_balance == 20_000
    assert by_sym["2330"].source == "twse"

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 2


@pytest.mark.asyncio
async def test_margin_empty_result_records_ok_zero(patch_margin_session):
    """Holiday / TWSE outage returns []. Cron is healthy, row_count=0."""
    from tasks import ingest_margin_tw

    with patch(
        "tasks.ingest_margin_tw.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.ingest_margin_tw.release_lock", AsyncMock(),
    ), patch(
        "tasks.ingest_margin_tw.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.ingest_margin_tw.clear_failures", AsyncMock(),
    ), patch(
        "tasks.ingest_margin_tw.twse.get_margin",
        AsyncMock(return_value=[]),
    ), patch(
        "tasks.ingest_margin_tw.record_health", AsyncMock(),
    ) as health:
        await ingest_margin_tw.run()

    kwargs = health.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["row_count"] == 0
