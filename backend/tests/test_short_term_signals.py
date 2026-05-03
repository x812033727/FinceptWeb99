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


# ── _compute_macd ─────────────────────────────────────────────────


def test_macd_returns_none_below_minimum_window():
    """MACD(12, 26, 9) needs 26 + 9 = 35 closes minimum to seed
    both EMAs and the signal smoother. Below that the signal line
    is unstable, so we surface None rather than a misleading value."""
    closes = [100.0 + i for i in range(20)]
    macd, sig, hist = sts._compute_macd(closes)
    assert macd is None and sig is None and hist is None


def test_macd_monotonic_uptrend_produces_positive_macd_line():
    """A strictly rising series → fast EMA leads slow EMA → MACD > 0.
    Note that `histogram` (= MACD − signal) tends toward 0 on
    perfectly LINEAR data because both lines stabilise to the same
    value; non-zero histogram only appears under acceleration. We
    pin the line, not the histogram, for the linear-trend case."""
    closes = [100.0 + i for i in range(50)]
    macd, sig, hist = sts._compute_macd(closes)
    assert macd is not None and sig is not None and hist is not None
    assert macd > 0
    assert sig > 0


def test_macd_monotonic_downtrend_produces_negative_macd_line():
    closes = [200.0 - i for i in range(50)]
    macd, sig, hist = sts._compute_macd(closes)
    assert macd is not None and sig is not None
    assert macd < 0
    assert sig < 0


def test_macd_acceleration_produces_positive_histogram():
    """Acceleration (price gains accelerating, not just steady) is
    when histogram diverges from zero. Quadratic ramp → fast EMA
    pulls away from slow EMA → MACD line rises faster than its
    own signal → histogram > 0. Pin this case so a refactor that
    breaks the histogram math (e.g. swapping MACD/signal) regresses
    loudly even when the line itself still looks right."""
    closes = [100.0 + i * i * 0.01 for i in range(50)]
    _, _, hist = sts._compute_macd(closes)
    assert hist is not None and hist > 0


# ── _compute_bollinger ────────────────────────────────────────────


def test_bollinger_returns_none_below_period():
    closes = [100.0] * 10
    pct_b, width = sts._compute_bollinger(closes, period=20)
    assert pct_b is None and width is None


def test_bollinger_zero_volatility_returns_none():
    """Constant prices → upper == lower → undefined %B. Return None
    so the caller treats it as 'no signal' (a stock that hasn't
    moved at all isn't actionable)."""
    closes = [100.0] * 30
    pct_b, width = sts._compute_bollinger(closes, period=20)
    assert pct_b is None and width is None


def test_bollinger_close_at_upper_band_pct_b_above_one():
    """Close that exceeds the +2σ upper band → %B > 1 (overbought
    breakout). Symmetric: a close below -2σ would give %B < 0."""
    closes = [100.0] * 19 + [120.0]   # last close is a huge spike
    pct_b, width = sts._compute_bollinger(closes, period=20)
    assert pct_b is not None and pct_b > 1.0
    assert width is not None and width > 0


def test_bollinger_close_at_mean_pct_b_near_half():
    """Close exactly at the SMA → %B ≈ 0.5 (mid-band)."""
    # 19 closes alternating 99/101 (mean 100) + final close at 100.
    closes = [99.0, 101.0] * 9 + [99.0, 100.0]
    assert closes[-1] == 100.0
    pct_b, _ = sts._compute_bollinger(closes, period=20)
    assert pct_b is not None
    assert 0.4 <= pct_b <= 0.6


# ── _compute_obv_5d_change_pct ────────────────────────────────────


def test_obv_returns_none_below_seven_bars():
    bars = [{"close": 100.0 + i, "volume": 1000} for i in range(5)]
    assert sts._compute_obv_5d_change_pct(bars) is None


def test_obv_strict_uptrend_with_constant_volume_grows_positive():
    """Every bar closes higher than the previous → OBV walk adds
    volume each step → cumulative grows monotonically → 5-day
    change is strongly positive."""
    bars = [
        {"close": 100.0 + i, "volume": 1000}
        for i in range(10)
    ]
    out = sts._compute_obv_5d_change_pct(bars)
    assert out is not None
    assert out > 0


def test_obv_strict_downtrend_with_constant_volume_grows_negative():
    """Every bar closes lower than the previous → OBV subtracts
    volume each step → cumulative grows more negative → 5-day
    change %, vs an already-negative anchor, is positive in
    MAGNITUDE but inverted by the sign flip in the formula:
    actually the 5-day window itself trends down so the change
    direction depends on the absolute value normalisation. Test
    that it's well-defined (not None) and matches the expected
    direction for a downtrend."""
    bars = [
        {"close": 200.0 - i, "volume": 1000}
        for i in range(10)
    ]
    out = sts._compute_obv_5d_change_pct(bars)
    assert out is not None
    # Downtrend → OBV is going more negative → latest < earlier.
    # (latest - earlier) is negative; abs(earlier) is positive.
    # So result is negative.
    assert out < 0


def test_obv_skips_zero_volume_or_null_close_bars_without_crashing():
    """Defensive: bad-data bars (null close / zero volume) get
    treated as 'hold' (OBV unchanged). The compute still produces
    a result as long as at least one valid up/down bar lands inside
    the 5-day window so the anchor isn't 0 (which would trigger
    div-by-zero protection)."""
    bars: list[dict] = [
        {"close": 100.0, "volume": 1000},
        {"close": 101.0, "volume": 1000},
        {"close": 102.0, "volume": 1000},
        {"close": 103.0, "volume": 1000},
        {"close": None,  "volume": 1000},   # null close → hold
        {"close": 105.0, "volume": 0},       # zero volume → hold
        {"close": 106.0, "volume": 1000},
        {"close": 107.0, "volume": 1000},
        {"close": 108.0, "volume": 1000},
        {"close": 109.0, "volume": 1000},
    ]
    out = sts._compute_obv_5d_change_pct(bars)
    assert out is not None  # didn't crash on the bad rows


# ── full-pipeline integration: compute_short_term_signals ─────────


@pytest.mark.asyncio
async def test_compute_includes_macd_bollinger_obv_in_signals(
    db_session: AsyncSession,
):
    """End-to-end: 50 monotonic-up bars must produce MACD / Bollinger
    / OBV fields populated alongside the existing RSI / KD output.
    The integration check that the new helpers wire into the
    `ShortTermSignals` dataclass + `to_dict` correctly — previously a
    field added to the dataclass that wasn't added to to_dict would
    silently drop the value at the API surface."""
    base = date(2026, 3, 1)
    await _seed(db_session, [
        _bar("2330", base + timedelta(days=i),
             close=600.0 + i, volume=1_000_000)
        for i in range(50)
    ])
    result = await sts.compute_short_term_signals(
        db_session, market="TW", symbol="2330",
        as_of=base + timedelta(days=49),
    )
    assert result is not None
    # All three new metric families must appear, with sensible signs
    # for a strict uptrend. Note: linear ramp converges histogram
    # toward 0 (both MACD and signal stabilise to the same value),
    # so we pin the line, not the histogram, for this case.
    assert result["macd"] is not None and result["macd"] > 0
    assert result["macd_hist"] is not None
    assert result["bollinger_pct_b"] is not None
    assert result["bollinger_width"] is not None and result["bollinger_width"] > 0
    assert result["obv_5d_change_pct"] is not None
    assert result["obv_5d_change_pct"] > 0


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
