"""Tests for `finmind.scheduler` — pure due-detection + DB-aware runner.

Two layers tested separately:
  - dispatcher: pure logic, no fixtures
  - runner:     end-to-end with FakeClient + the in-memory session
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from finmind.models.dataset_source import DatasetSource
from finmind.models.master import TwStockInfo
from finmind.scheduler.dispatcher import (
    expand_due_datasets,
    is_due,
    suggested_range,
)
from finmind.scheduler.runner import (
    get_universe_from_tw_stock_info,
    run_due_now,
)
from finmind.scripts.init_db import seed_dataset_sources


# ── Pure dispatcher ─────────────────────────────────────────────


def test_is_due_when_never_run():
    """NULL last_ingest_at → due immediately. First-run after deploy
    must fire ASAP, not wait a full cadence."""
    now = datetime.now(tz=UTC)
    assert is_due("daily", None, now) is True
    assert is_due("monthly", None, now) is True
    assert is_due("quarterly", None, now) is True


def test_is_due_when_within_budget():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    fresh = now - timedelta(hours=6)
    assert is_due("daily", fresh, now) is False  # within 22h budget


def test_is_due_when_past_budget():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    stale = now - timedelta(hours=30)  # > 22h budget
    assert is_due("daily", stale, now) is True


def test_is_due_per_cadence_thresholds():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    # Monthly budget ≈ 28 days
    assert is_due("monthly", now - timedelta(days=20), now) is False
    assert is_due("monthly", now - timedelta(days=30), now) is True
    # Quarterly budget ≈ 88 days
    assert is_due("quarterly", now - timedelta(days=60), now) is False
    assert is_due("quarterly", now - timedelta(days=100), now) is True


def test_realtime_and_on_demand_never_due():
    """These cadences are intentionally excluded from auto-runs."""
    now = datetime.now(tz=UTC)
    assert is_due("realtime", None, now) is False
    assert is_due("on_demand", None, now) is False


def test_unknown_cadence_returns_not_due():
    """Garbage `ingest_freq` value must NOT silently fire every poll —
    operator should see no run + then notice the misconfiguration."""
    now = datetime.now(tz=UTC)
    assert is_due("definitely_not_a_cadence", None, now) is False


def test_is_due_handles_naive_datetime():
    """SQLite returns naive datetimes; the function should treat them
    as UTC, not crash."""
    now = datetime.now(tz=UTC)
    naive_stale = (now - timedelta(hours=30)).replace(tzinfo=None)
    assert is_due("daily", naive_stale, now) is True


def test_suggested_range_overlaps_previous_run():
    """Daily cadence backfills the last 7 days — overlap with previous
    run is intentional (UPSERT idempotent; covers late corrections +
    weekend gaps)."""
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    rs, re = suggested_range("daily", now)
    assert re == date(2024, 5, 1)
    assert (re - rs).days == 7


def test_suggested_range_per_cadence():
    now = datetime(2024, 5, 1, 12, 0, tzinfo=UTC)
    assert (suggested_range("monthly", now)[1] - suggested_range("monthly", now)[0]).days == 90
    assert (suggested_range("quarterly", now)[1] - suggested_range("quarterly", now)[0]).days == 180


# ── expand_due_datasets ─────────────────────────────────────────


def _make_ds(**kwargs) -> DatasetSource:
    """Construct a DatasetSource for test purposes — fills sane
    defaults for the columns we don't care about per-test."""
    defaults = {
        "dataset_code": "TestDataset",
        "category": "technical",
        "description_zh": "test",
        "local_table": "ohlcv_daily",
        "per_symbol": False,
        "primary_source": "finmind",
        "fallback_source": "twse",
        "active_source": "finmind",
        "sponsor_tier": False,
        "enabled": True,
        "ingest_freq": "daily",
        "last_ingest_at": None,
        "last_ingest_rows": None,
        "last_error": None,
    }
    defaults.update(kwargs)
    return DatasetSource(**defaults)


def test_expand_skips_disabled_datasets():
    now = datetime.now(tz=UTC)
    rows = [
        _make_ds(dataset_code="A", enabled=False),
        _make_ds(dataset_code="B", enabled=True),
    ]
    chunks = expand_due_datasets(rows, now)
    assert {c.dataset_code for c in chunks} == {"B"}


def test_expand_skips_unbuilt_local_table():
    """A dataset enabled by mistake before its destination table was
    migrated would otherwise UPSERT into '' — skip cleanly."""
    now = datetime.now(tz=UTC)
    rows = [
        _make_ds(dataset_code="A", local_table=""),
    ]
    chunks = expand_due_datasets(rows, now)
    assert chunks == []


def test_expand_skips_fresh_datasets():
    now = datetime.now(tz=UTC)
    rows = [
        _make_ds(dataset_code="Stale", last_ingest_at=now - timedelta(hours=30)),
        _make_ds(dataset_code="Fresh", last_ingest_at=now - timedelta(hours=2)),
    ]
    chunks = expand_due_datasets(rows, now)
    assert {c.dataset_code for c in chunks} == {"Stale"}


def test_expand_per_symbol_fans_across_universe():
    """Per-symbol datasets get one DueChunk per (dataset, symbol)
    when a universe is supplied."""
    now = datetime.now(tz=UTC)
    rows = [
        _make_ds(dataset_code="P", per_symbol=True),
    ]
    chunks = expand_due_datasets(rows, now, symbols=["2330", "2317", "2454"])
    assert len(chunks) == 3
    assert {c.symbol for c in chunks} == {"2330", "2317", "2454"}
    assert all(c.dataset_code == "P" for c in chunks)


def test_expand_per_symbol_with_no_universe_emits_market_wide_call():
    """Without a universe, per-symbol datasets fall through to
    symbol=None — most upstream sources will fail this, the runner
    records 'failed', operator sees the issue."""
    now = datetime.now(tz=UTC)
    rows = [_make_ds(dataset_code="P", per_symbol=True)]
    chunks = expand_due_datasets(rows, now, symbols=None)
    assert len(chunks) == 1
    assert chunks[0].symbol is None


def test_expand_market_wide_ignores_universe():
    """Market-wide datasets emit one chunk regardless of the symbols
    list (the symbols are only relevant for per-symbol datasets)."""
    now = datetime.now(tz=UTC)
    rows = [_make_ds(dataset_code="M", per_symbol=False)]
    chunks = expand_due_datasets(rows, now, symbols=["2330", "2317"])
    assert len(chunks) == 1
    assert chunks[0].symbol is None


# ── run_due_now (DB + ingest_chunk) ─────────────────────────────


@dataclass
class FakeClient:
    rows_per_call: list[dict[str, Any]]
    raise_on_call: Exception | None = None
    call_count: int = 0

    async def fetch(self, dataset_code, symbol, start_date, end_date):
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.rows_per_call)


@pytest.mark.asyncio
async def test_run_due_now_invokes_only_enabled_due_datasets(finmind_session):
    """Seed the catalog, enable one dataset, leave the others off →
    only that one runs."""
    await seed_dataset_sources()

    # Enable TaiwanStockPrice (per-symbol). Don't supply symbols →
    # runner emits 1 chunk with symbol=None which will fail at the
    # FinMind layer; here we mock that with a no-op FakeClient.
    from finmind.models.dataset_source import DatasetSource as DS
    row = await finmind_session.get(DS, "TaiwanStockPrice")
    row.enabled = True
    await finmind_session.commit()

    fake = FakeClient(rows_per_call=[])
    outcomes = await run_due_now(finmind_session, client=fake)

    assert len(outcomes) == 1
    assert outcomes[0].chunk.dataset_code == "TaiwanStockPrice"
    # Empty FakeClient → 0 rows, status 'done'.
    assert outcomes[0].result.status == "done"
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_run_due_now_skips_when_no_datasets_enabled(finmind_session):
    """Default catalog state — every dataset enabled=False → nothing
    to run. Healthy no-op."""
    await seed_dataset_sources()

    outcomes = await run_due_now(finmind_session, client=FakeClient([]))
    assert outcomes == []


@pytest.mark.asyncio
async def test_run_due_now_filters_enabled_datasets_by_category(
    finmind_session,
):
    """The crypto cron must not run enabled Taiwan-market datasets."""
    await seed_dataset_sources()

    for code in ("TaiwanStockPrice", "CryptoPrice"):
        row = await finmind_session.get(DatasetSource, code)
        row.enabled = True
    await finmind_session.commit()

    fake = FakeClient(rows_per_call=[])
    outcomes = await run_due_now(
        finmind_session,
        crypto_symbols=["BTCUSDT"],
        categories={"crypto"},
        client=fake,
    )

    assert outcomes
    assert {outcome.chunk.dataset_code for outcome in outcomes} == {
        "CryptoPrice"
    }
    assert {outcome.chunk.symbol for outcome in outcomes} == {"BTCUSDT"}
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_run_due_now_empty_category_filter_is_a_noop(finmind_session):
    await seed_dataset_sources()
    row = await finmind_session.get(DatasetSource, "CryptoPrice")
    row.enabled = True
    await finmind_session.commit()

    fake = FakeClient(rows_per_call=[])
    outcomes = await run_due_now(
        finmind_session, categories=set(), client=fake
    )

    assert outcomes == []
    assert fake.call_count == 0


@pytest.mark.asyncio
async def test_run_due_now_fans_out_per_symbol_dataset(finmind_session):
    """Enable a per-symbol dataset + supply a universe → one ingest
    call per symbol."""
    await seed_dataset_sources()

    from finmind.models.dataset_source import DatasetSource as DS
    row = await finmind_session.get(DS, "TaiwanStockPrice")
    row.enabled = True
    await finmind_session.commit()

    fake = FakeClient(rows_per_call=[])
    outcomes = await run_due_now(
        finmind_session, symbols=["2330", "2317", "2454"], client=fake,
    )

    assert len(outcomes) == 3
    assert {o.chunk.symbol for o in outcomes} == {"2330", "2317", "2454"}
    assert fake.call_count == 3


@pytest.mark.asyncio
async def test_run_due_now_continues_after_chunk_failure(finmind_session):
    """One bad dataset must NOT cancel the rest of the sweep —
    operator wants to see the full picture per run."""
    await seed_dataset_sources()

    from finmind.models.dataset_source import DatasetSource as DS
    # Enable two datasets so both end up in the work list.
    for code in ("TaiwanStockPrice", "TaiwanStockMarginPurchaseShortSale"):
        row = await finmind_session.get(DS, code)
        row.enabled = True
    await finmind_session.commit()

    fake = FakeClient(rows_per_call=[], raise_on_call=RuntimeError("boom"))
    outcomes = await run_due_now(finmind_session, client=fake)

    # Both datasets attempted, both failed — neither short-circuited
    # the other.
    assert len(outcomes) == 2
    statuses = [o.result.status for o in outcomes]
    assert all(s == "failed" for s in statuses)


@pytest.mark.asyncio
async def test_run_due_now_redacts_unexpected_scheduler_errors(
    finmind_session, monkeypatch, caplog,
):
    await seed_dataset_sources()
    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.enabled = True
    await finmind_session.commit()
    secret = "test-scheduler-secret"

    async def crash_ingest(*_args, **_kwargs):
        raise RuntimeError(
            "scheduler failure https://example.test/data?"
            f"token={secret}&dataset=TaiwanStockPrice"
        )

    monkeypatch.setattr(
        "finmind.scheduler.runner.ingest_chunk", crash_ingest
    )

    outcomes = await run_due_now(finmind_session, client=FakeClient([]))

    assert len(outcomes) == 1
    assert outcomes[0].result.status == "failed"
    assert outcomes[0].result.error is not None
    assert secret not in outcomes[0].result.error
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_run_due_now_skips_fresh_dataset(finmind_session):
    """A dataset whose last_ingest_at is fresh (< budget) is filtered
    out by the dispatcher — runner doesn't even attempt the fetch."""
    await seed_dataset_sources()

    from finmind.models.dataset_source import DatasetSource as DS
    row = await finmind_session.get(DS, "TaiwanStockPrice")
    row.enabled = True
    row.last_ingest_at = datetime.now(tz=UTC)  # just ran
    await finmind_session.commit()

    fake = FakeClient(rows_per_call=[])
    outcomes = await run_due_now(finmind_session, client=fake)
    assert outcomes == []
    assert fake.call_count == 0


# ── Universe loader from tw_stock_info ──────────────────────────


@pytest.mark.asyncio
async def test_universe_loader_returns_empty_when_no_rows(finmind_session):
    """Fresh DB — no TaiwanStockInfo ingest yet → empty list. Caller
    decides whether to fall back or skip; helper doesn't crash."""
    universe = await get_universe_from_tw_stock_info(finmind_session)
    assert universe == []


@pytest.mark.asyncio
async def test_universe_loader_returns_sorted_symbols(finmind_session):
    """Insert a few rows (out of order) and verify the helper returns
    them sorted — deterministic ordering matters for log capture +
    progress comparability across runs."""
    finmind_session.add_all([
        TwStockInfo(market="TWSE", symbol="2330",
                    name_zh="台積電", source="finmind"),
        TwStockInfo(market="TWSE", symbol="2317",
                    name_zh="鴻海", source="finmind"),
        TwStockInfo(market="OTC", symbol="6446",
                    name_zh="藥華", source="finmind"),
    ])
    await finmind_session.commit()

    universe = await get_universe_from_tw_stock_info(finmind_session)
    assert universe == ["2317", "2330", "6446"]


@pytest.mark.asyncio
async def test_universe_loader_filters_by_market(finmind_session):
    finmind_session.add_all([
        TwStockInfo(market="TWSE", symbol="2330", source="finmind"),
        TwStockInfo(market="OTC", symbol="6446", source="finmind"),
    ])
    await finmind_session.commit()

    twse_only = await get_universe_from_tw_stock_info(
        finmind_session, market="TWSE"
    )
    assert twse_only == ["2330"]

    otc_only = await get_universe_from_tw_stock_info(
        finmind_session, market="OTC"
    )
    assert otc_only == ["6446"]


@pytest.mark.asyncio
async def test_universe_loader_excludes_warrants_by_default(finmind_session):
    """Warrants dwarf the equity universe — default-exclude so the
    daily cron doesn't fan out into thousands of irrelevant calls."""
    finmind_session.add_all([
        TwStockInfo(market="TWSE", symbol="2330",
                    is_warrant=False, source="finmind"),
        TwStockInfo(market="TWSE", symbol="081234",
                    is_warrant=True, source="finmind"),
    ])
    await finmind_session.commit()

    default = await get_universe_from_tw_stock_info(finmind_session)
    assert default == ["2330"]

    with_warrants = await get_universe_from_tw_stock_info(
        finmind_session, exclude_warrants=False,
    )
    assert with_warrants == ["081234", "2330"]


@pytest.mark.asyncio
async def test_run_due_now_uses_universe_loader_results(finmind_session):
    """End-to-end: TaiwanStockInfo populated → loader returns symbols
    → run_due_now fans the per-symbol dataset across them."""
    await seed_dataset_sources()

    finmind_session.add_all([
        TwStockInfo(market="TWSE", symbol="2330", source="finmind"),
        TwStockInfo(market="TWSE", symbol="2317", source="finmind"),
    ])
    from finmind.models.dataset_source import DatasetSource as DS
    row = await finmind_session.get(DS, "TaiwanStockPrice")
    row.enabled = True
    await finmind_session.commit()

    universe = await get_universe_from_tw_stock_info(finmind_session)
    assert universe == ["2317", "2330"]

    fake = FakeClient(rows_per_call=[])
    outcomes = await run_due_now(
        finmind_session, symbols=universe, client=fake,
    )

    assert len(outcomes) == 2
    assert {o.chunk.symbol for o in outcomes} == {"2317", "2330"}
