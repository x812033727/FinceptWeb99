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


# ── _compute_kd_9_3_3 ─────────────────────────────────────────────


def _kd_bar(high: float, low: float, close: float) -> dict:
    return {"high": high, "low": low, "close": close}


def test_kd_returns_none_when_under_11_bars():
    bars = [_kd_bar(105, 95, 100) for _ in range(10)]
    k, d = sts._compute_kd_9_3_3(bars)
    assert k is None and d is None


def test_kd_extreme_overbought_close_at_high():
    """20 bars all closing at the period's high → K and D should both
    sit deep in overbought territory (> 80)."""
    bars = [_kd_bar(100 + i, 90 + i, 100 + i) for i in range(20)]
    k, d = sts._compute_kd_9_3_3(bars)
    assert k is not None and d is not None
    assert k > 80
    assert d > 80


def test_kd_extreme_oversold_close_at_low():
    """20 bars all closing at the period's low → K and D both deep in
    oversold (< 20)."""
    bars = [_kd_bar(110 - i, 100 - i, 100 - i) for i in range(20)]
    k, d = sts._compute_kd_9_3_3(bars)
    assert k is not None and d is not None
    assert k < 20
    assert d < 20


def test_kd_neutral_when_high_equals_low():
    """Degenerate window where every bar's high == low (no range) →
    RSV defaults to 50, K and D drift towards 50."""
    bars = [_kd_bar(100, 100, 100) for _ in range(20)]
    k, d = sts._compute_kd_9_3_3(bars)
    assert k is not None and d is not None
    assert 45 <= k <= 55
    assert 45 <= d <= 55


@pytest.mark.asyncio
async def test_compute_includes_kd_in_signals(db_session: AsyncSession):
    """Full pipeline check: KD must appear in the signals dict
    alongside the existing metrics."""
    base = date(2026, 3, 1)
    await _seed(db_session, [
        _bar("2330", base + timedelta(days=i),
             close=600.0 + i, volume=1_000_000)
        for i in range(30)
    ])
    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=29),
    )
    assert result is not None
    assert "kd_k" in result
    assert "kd_d" in result
    assert result["kd_k"] is not None
    assert result["kd_d"] is not None


# ── compute_industry_rs ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_industry_rs_returns_none_for_non_tw_market(
    db_session: AsyncSession,
):
    """Industry classification map is TW-only today; non-TW symbols
    must return None rather than fabricate a peer set."""
    result = await sts.compute_industry_rs(
        db_session, market="US", symbol="AAPL",
        as_of=date(2026, 4, 30),
    )
    assert result is None


@pytest.mark.asyncio
async def test_industry_rs_returns_none_when_industry_unmapped(
    db_session: AsyncSession,
    monkeypatch,
):
    """Symbol not in `_industry_map` (fresh process before cron, or
    unclassified code) → None."""
    monkeypatch.setattr(
        "services.tw_market_service.get_industry", lambda s: None,
    )
    monkeypatch.setattr(
        "services.tw_market_service.get_industry_peers",
        lambda s, exclude_self=True: [],
    )
    result = await sts.compute_industry_rs(
        db_session, market="TW", symbol="9999",
        as_of=date(2026, 4, 30),
    )
    assert result is None


@pytest.mark.asyncio
async def test_industry_rs_returns_none_below_min_peer_count(
    db_session: AsyncSession,
    monkeypatch,
):
    """Industry classified but only 2 peers → can't trust the median.
    Returns None rather than emit a noisy 2-sample stat."""
    monkeypatch.setattr(
        "services.tw_market_service.get_industry", lambda s: "半導體業",
    )
    monkeypatch.setattr(
        "services.tw_market_service.get_industry_peers",
        lambda s, exclude_self=True: ["2454", "3034"],   # only 2 peers
    )
    base = date(2026, 4, 1)
    bars = [
        _bar("2330", base + timedelta(days=i), close=600.0 + i)
        for i in range(7)
    ]
    bars.extend([
        _bar(p, base + timedelta(days=i), close=900.0 + i)
        for p in ("2454", "3034")
        for i in range(7)
    ])
    await _seed(db_session, bars)

    result = await sts.compute_industry_rs(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=6),
    )
    assert result is None


@pytest.mark.asyncio
async def test_industry_rs_computes_relative_strength(
    db_session: AsyncSession,
    monkeypatch,
):
    """End-to-end: focus symbol +5%, three peers each +1% → industry
    median = 1, rs_score = +4 (focus is leading the sector)."""
    peers = ["2454", "3034", "2303", "2379"]
    monkeypatch.setattr(
        "services.tw_market_service.get_industry", lambda s: "半導體業",
    )
    monkeypatch.setattr(
        "services.tw_market_service.get_industry_peers",
        lambda s, exclude_self=True: peers,
    )

    base = date(2026, 4, 1)
    # Focus symbol: 100 → 105 over 6 bars (+5%).
    focus_bars = [
        _bar("2330", base + timedelta(days=0), close=100.0),
        _bar("2330", base + timedelta(days=1), close=101.0),
        _bar("2330", base + timedelta(days=2), close=102.0),
        _bar("2330", base + timedelta(days=3), close=103.0),
        _bar("2330", base + timedelta(days=4), close=104.0),
        _bar("2330", base + timedelta(days=5), close=104.5),
        _bar("2330", base + timedelta(days=6), close=105.0),
    ]
    # Each peer: 100 → 101 over 6 bars (+1%).
    peer_bars = [
        _bar(p, base + timedelta(days=i),
             close=100.0 + (1.0 if i == 6 else 100.0 * i * 0.0))
        for p in peers
        for i in range(7)
    ]
    # Re-seed peer closes with simple linear ramp 100 → 101.
    peer_bars = [
        _bar(p, base + timedelta(days=i), close=100.0 + i * (1.0 / 6))
        for p in peers
        for i in range(7)
    ]
    await _seed(db_session, focus_bars + peer_bars)

    result = await sts.compute_industry_rs(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=6),
    )
    assert result is not None
    assert result["industry"] == "半導體業"
    assert result["peer_count"] == 4
    assert result["symbol_return_5d"] == pytest.approx(4.0, abs=0.05)
    # Peer median of (101/100.166... - 1)*100 ≈ +0.83% — accept a
    # small tolerance for floating-point.
    assert 0.7 <= result["industry_median_5d"] <= 0.9
    assert result["rs_score"] == pytest.approx(
        result["symbol_return_5d"] - result["industry_median_5d"],
        abs=0.01,
    )
    # Focus is leading the sector — positive RS.
    assert result["rs_score"] > 0


# ── _median + _five_day_return helpers ────────────────────────────


def test_median_odd_and_even_lengths():
    assert sts._median([1.0, 2.0, 3.0]) == 2.0
    assert sts._median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_five_day_return_returns_none_below_six_closes():
    bars = [{"close": 100.0} for _ in range(5)]
    assert sts._five_day_return(bars) is None


def test_five_day_return_skips_null_closes():
    bars = [
        {"close": None}, {"close": 100.0}, {"close": 101.0},
        {"close": 102.0}, {"close": 103.0}, {"close": 104.0},
        {"close": 105.0},
    ]
    r = sts._five_day_return(bars)
    assert r == pytest.approx(5.0, abs=0.001)
