"""Unit tests for the A2 intraday aggregator (services.intraday_service).

`aggregate_snapshot_bars` is a pure function so bucket-edge, first/last,
and cumulative-volume-differencing semantics are pinned down directly on
hand-built tick series; `get_intraday` is exercised against seeded
quote_snapshots rows through the autosession read path.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import services.intraday_service as svc
from services.ingest.repository import QuoteSnapshotRow, insert_quote_snapshot
from tasks.ingest_quotes_retention_tw import RETENTION_DAYS


def _ts(h: int, m: int, s: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 7, day, h, m, s, tzinfo=UTC)


# ── pure aggregation ──────────────────────────────────────────────

def test_bucket_edges_1m():
    """09:00:00 and 09:00:59 share a 1m bucket; 09:01:00 starts the next."""
    rows = [
        (_ts(9, 0, 0), 100.0, 1000),
        (_ts(9, 0, 59), 101.0, 1500),
        (_ts(9, 1, 0), 102.0, 1800),
    ]
    bars = svc.aggregate_snapshot_bars(rows, 60)
    assert len(bars) == 2
    assert bars[0]["time"] == int(_ts(9, 0).timestamp()) * 1000
    assert bars[1]["time"] == int(_ts(9, 1).timestamp()) * 1000


def test_first_last_open_close_and_high_low():
    """open = first tick in bucket, close = last; high/low span the bucket."""
    rows = [
        (_ts(9, 0, 0), 100.0, 1000),
        (_ts(9, 1, 0), 105.0, 2000),
        (_ts(9, 2, 0), 95.0, 3000),
        (_ts(9, 3, 0), 102.0, 4000),
    ]
    bars = svc.aggregate_snapshot_bars(rows, 300)  # single 5m bucket
    assert len(bars) == 1
    b = bars[0]
    assert b["open"] == 100.0
    assert b["close"] == 102.0
    assert b["high"] == 105.0
    assert b["low"] == 95.0


def test_volume_is_cumulative_diff_within_day():
    """Snapshot volume is cumulative session volume → per-bar volume is the
    bucket-to-bucket difference; the first bar reports the raw cumulative."""
    rows = [
        (_ts(9, 0, 0), 100.0, 1000),
        (_ts(9, 0, 30), 100.5, 1200),
        (_ts(9, 1, 0), 101.0, 1500),
        (_ts(9, 2, 0), 101.5, 1500),   # no trades this minute
    ]
    bars = svc.aggregate_snapshot_bars(rows, 60)
    assert [b["volume"] for b in bars] == [1200, 300, 0]


def test_volume_resets_across_day_boundary():
    """A new UTC day must not difference against yesterday's cumulative —
    the first bar of the day reports its own cumulative volume."""
    rows = [
        (_ts(9, 0, 0, day=10), 100.0, 50_000),
        (_ts(9, 0, 0, day=11), 102.0, 200),
        (_ts(9, 1, 0, day=11), 103.0, 700),
    ]
    bars = svc.aggregate_snapshot_bars(rows, 60)
    assert [b["volume"] for b in bars] == [50_000, 200, 500]


def test_volume_regression_clamped_to_zero():
    """Upstream corrections can briefly regress the cumulative counter —
    clamp at 0 instead of emitting a negative bar."""
    rows = [
        (_ts(9, 0, 0), 100.0, 1000),
        (_ts(9, 1, 0), 100.5, 800),   # regression
        (_ts(9, 2, 0), 101.0, 1600),
    ]
    bars = svc.aggregate_snapshot_bars(rows, 60)
    assert bars[1]["volume"] == 0
    # Next diff is taken against the latest observed cumulative (800).
    assert bars[2]["volume"] == 800


def test_none_price_ticks_are_skipped_and_none_volume_is_zero():
    rows = [
        (_ts(9, 0, 0), None, 999),        # skipped entirely
        (_ts(9, 1, 0), 100.0, None),      # bar exists, volume 0
        (_ts(9, 2, 0), 101.0, 300),
    ]
    bars = svc.aggregate_snapshot_bars(rows, 60)
    assert len(bars) == 2
    assert bars[0]["volume"] == 0
    # No prior cumulative observed this day → first real cumulative is raw.
    assert bars[1]["volume"] == 300


def test_empty_input():
    assert svc.aggregate_snapshot_bars([], 60) == []


# ── service round-trip against seeded snapshots ──────────────────

def _snap(symbol: str, ts: datetime, price: float, volume: int,
          market: str = "TW") -> QuoteSnapshotRow:
    return QuoteSnapshotRow(
        market=market, symbol=symbol, ts=ts,
        last_price=price, change_pct=0.1, prev_close=price - 1,
        volume=volume, source="twse",
    )


@pytest.mark.asyncio
async def test_get_intraday_aggregates_seeded_snapshots(db_session: AsyncSession):
    # Fixed intra-day time (03:00 UTC yesterday) — always inside the
    # coverage window and never straddling a UTC midnight, which would
    # trigger the day-boundary volume reset and flake the assertion.
    base = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=3, minute=0, second=0, microsecond=0,
    )
    for i, (price, vol) in enumerate([(600.0, 1000), (601.0, 1400), (599.5, 2000)]):
        await insert_quote_snapshot(
            db_session, _snap("INTR1", base + timedelta(minutes=i), price, vol),
        )

    out = await svc.get_intraday("TW", "INTR1", "1m")
    assert out["symbol"] == "INTR1"
    assert out["market"] == "TW"
    assert out["interval"] == "1m"
    assert out["coverage_days"] == RETENTION_DAYS
    assert len(out["bars"]) == 3
    assert out["bars"][0]["open"] == 600.0
    assert [b["volume"] for b in out["bars"]] == [1000, 400, 600]


@pytest.mark.asyncio
async def test_get_intraday_empty_when_no_snapshots():
    out = await svc.get_intraday("TW", "INTR_NONE", "5m")
    assert out["bars"] == []
    assert out["coverage_days"] == RETENTION_DAYS


@pytest.mark.asyncio
async def test_get_intraday_scopes_by_market(db_session: AsyncSession):
    """A TW snapshot must not leak into the US intraday response."""
    now = datetime.now(UTC)
    await insert_quote_snapshot(db_session, _snap("INTR2", now, 100.0, 500, market="TW"))

    us = await svc.get_intraday("US", "INTR2", "1m")
    tw = await svc.get_intraday("TW", "INTR2", "1m")
    assert us["bars"] == []
    assert len(tw["bars"]) == 1


@pytest.mark.asyncio
async def test_get_intraday_excludes_rows_beyond_coverage(db_session: AsyncSession):
    """Rows older than the retention window are not aggregated even if the
    prune hasn't deleted them yet."""
    now = datetime.now(UTC)
    await insert_quote_snapshot(
        db_session, _snap("INTR3", now - timedelta(days=RETENTION_DAYS + 2), 90.0, 100),
    )
    await insert_quote_snapshot(db_session, _snap("INTR3", now, 100.0, 500))

    out = await svc.get_intraday("TW", "INTR3", "15m")
    assert len(out["bars"]) == 1
    assert out["bars"][0]["close"] == 100.0


def test_coverage_matches_retention_policy():
    """The coverage constant must track the prune task's retention window."""
    assert svc.SNAPSHOT_COVERAGE_DAYS == RETENTION_DAYS
