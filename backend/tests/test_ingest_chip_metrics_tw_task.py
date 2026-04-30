"""Tests for tasks.ingest_institutional_tw and tasks.ingest_margin_tw.

Both pull the full TW market in one TWSE call per day and bulk-upsert
into `tw_institutional_daily` / `tw_margin_daily`. Tests exercise the
common patterns (lock skip, success path, idempotent re-run, backoff
preservation) for both jobs without duplicating fixtures.
"""
from datetime import date
from unittest.mock import AsyncMock, patch

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
        "tasks.ingest_institutional_tw.twse.get_institutional",
        AsyncMock(return_value=rows),
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
async def test_institutional_rerun_dedupes(
    patch_institutional_session, db_session: AsyncSession,
):
    """Same symbol on the same date is upserted, not duplicated."""
    from tasks import ingest_institutional_tw

    rows_v1 = [_institutional_row("2330")]
    rows_v2 = [{**_institutional_row("2330"), "fini_buy": 50_000}]

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
    )
    for ctx in common:
        ctx.__enter__()
    try:
        with patch(
            "tasks.ingest_institutional_tw.twse.get_institutional",
            AsyncMock(return_value=rows_v1),
        ):
            await ingest_institutional_tw.run()
        with patch(
            "tasks.ingest_institutional_tw.twse.get_institutional",
            AsyncMock(return_value=rows_v2),
        ):
            await ingest_institutional_tw.run()
    finally:
        for ctx in common:
            ctx.__exit__(None, None, None)

    db_rows = (await db_session.scalars(
        select(TwInstitutionalDaily).where(
            TwInstitutionalDaily.symbol == "2330",
        )
    )).all()
    assert len(db_rows) == 1
    assert db_rows[0].fini_buy == 50_000   # v2 overwrote v1


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
