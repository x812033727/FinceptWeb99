"""Pure-compute tests for `services.short_term_signals`.

Seeds an in-memory `ohlcv_daily` table with synthetic bars, calls
`compute_short_term_signals`, asserts on the metric values. No HTTP,
no Redis — just SQL + math.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from services import short_term_signals as sts
from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars


# ── helpers ────────────────────────────────────────────────────────


def _bar(
    symbol: str,
    ts: date,
    *,
    close: float,
    volume: int = 1_000_000,
    open_: float | None = None,
) -> OhlcvBar:
    return OhlcvBar(
        market="TW", symbol=symbol, ts=ts,
        open=open_ if open_ is not None else close,
        high=close + 1, low=close - 1, close=close,
        volume=volume, source="test",
    )


async def _seed(db: AsyncSession, bars: list[OhlcvBar]) -> None:
    await upsert_ohlcv_bars(db, bars)


# ── _compute_rsi_14 ────────────────────────────────────────────────


def test_rsi_returns_none_below_15_closes():
    assert sts._compute_rsi_14([100.0] * 14) is None


def test_rsi_all_up_days_caps_at_100():
    closes = [100.0 + i for i in range(20)]   # strictly increasing
    rsi = sts._compute_rsi_14(closes)
    assert rsi == 100.0


def test_rsi_all_down_days_returns_zero():
    closes = [200.0 - i for i in range(20)]   # strictly decreasing
    rsi = sts._compute_rsi_14(closes)
    assert rsi == 0.0


def test_rsi_balanced_around_50():
    """Equal-magnitude alternating up/down deltas → RSI near 50."""
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
    rsi = sts._compute_rsi_14(closes)
    assert rsi is not None
    assert 40.0 <= rsi <= 60.0


# ── _compute_volume_ratio ─────────────────────────────────────────


def test_volume_ratio_basic():
    """Today=2x of trailing mean → ratio == 2.0."""
    today = 2_000_000
    prior = [1_000_000] * 20
    assert sts._compute_volume_ratio(today, prior) == 2.0


def test_volume_ratio_returns_none_when_today_missing():
    assert sts._compute_volume_ratio(None, [1_000_000] * 20) is None


def test_volume_ratio_returns_none_when_history_all_zero():
    """Freshly-IPO'd stock: trailing mean = 0 → can't divide."""
    assert sts._compute_volume_ratio(1_000_000, [0] * 20) is None


def test_volume_ratio_skips_null_history_entries():
    """Some bars in the window may have null volume (data gap). The
    helper averages over the non-null subset so a single missing bar
    doesn't blank the ratio."""
    today = 2_000_000
    prior: list[int | None] = [1_000_000] * 19 + [None]
    assert sts._compute_volume_ratio(today, prior) == 2.0


# ── compute_short_term_signals end-to-end ─────────────────────────


@pytest.mark.asyncio
async def test_compute_returns_none_when_archive_too_short(
    db_session: AsyncSession,
):
    """< 21 bars → not enough history for averages, return None
    rather than emit half-baked metrics."""
    base = date(2026, 4, 1)
    await _seed(db_session, [
        _bar("2330", base + timedelta(days=i), close=600.0 + i)
        for i in range(10)   # only 10 bars
    ])
    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=10),
    )
    assert result is None


@pytest.mark.asyncio
async def test_compute_returns_full_metrics_for_seeded_bars(
    db_session: AsyncSession,
):
    """30 bars of monotonic-up price + flat volume EXCEPT today=3x.
    Verify each metric matches the math:
      - volume_ratio ≈ 3.0
      - return_5d positive
      - return_20d positive
      - rsi_14 = 100 (all up days)
      - gap_pct = 0 (open == prev close)
    """
    base = date(2026, 3, 1)
    bars = [
        _bar("2330", base + timedelta(days=i),
             close=600.0 + i,
             volume=1_000_000)
        for i in range(30)
    ]
    # Today's bar uses 3x volume.
    today_idx = 29
    bars[today_idx] = _bar(
        "2330", base + timedelta(days=today_idx),
        close=600.0 + today_idx,
        open_=600.0 + today_idx - 1,   # gap == 0 (open == prev close)
        volume=3_000_000,
    )
    await _seed(db_session, bars)

    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=29),
    )
    assert result is not None
    assert result["volume_ratio"] == 3.0
    assert result["return_5d"] is not None
    assert result["return_5d"] > 0
    assert result["return_20d"] is not None
    assert result["return_20d"] > 0
    assert result["rsi_14"] == 100.0
    assert result["gap_pct"] == 0.0
    assert result["close"] == 629.0


@pytest.mark.asyncio
async def test_compute_gap_pct_detects_open_above_prev_close(
    db_session: AsyncSession,
):
    """Today opens 5% above yesterday's close → gap_pct ≈ 5."""
    base = date(2026, 3, 1)
    bars = [
        _bar("2330", base + timedelta(days=i), close=100.0)
        for i in range(28)
    ]
    bars.append(_bar(
        "2330", base + timedelta(days=28),
        close=100.0, volume=1_000_000,
    ))
    bars.append(_bar(
        "2330", base + timedelta(days=29),
        close=110.0, open_=105.0, volume=1_000_000,
    ))
    await _seed(db_session, bars)

    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=29),
    )
    assert result is not None
    assert result["gap_pct"] == 5.0


@pytest.mark.asyncio
async def test_compute_anchor_excludes_future_bars(
    db_session: AsyncSession,
):
    """Backtest mode: `as_of` cutoffs the read window so today's
    bars (in real life unknown at as_of) don't leak into the past
    metrics."""
    base = date(2026, 3, 1)
    # 25 bars of $100 leading up to the anchor + 3 bars of $200 AFTER.
    bars = [
        _bar("2330", base + timedelta(days=i), close=100.0)
        for i in range(25)
    ]
    bars.extend([
        _bar("2330", base + timedelta(days=25 + i), close=200.0)
        for i in range(3)
    ])
    await _seed(db_session, bars)

    # Anchor at day-24; the post-anchor $200 bars must NOT influence
    # metrics.
    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=24),
    )
    assert result is not None
    assert result["close"] == 100.0
    assert result["return_5d"] == 0.0
    assert result["return_20d"] == 0.0


@pytest.mark.asyncio
async def test_compute_returns_none_for_unknown_symbol(
    db_session: AsyncSession,
):
    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="DOES_NOT_EXIST",
        as_of=date(2026, 4, 30),
    )
    assert result is None
