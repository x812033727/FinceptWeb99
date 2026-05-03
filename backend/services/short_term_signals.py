"""Short-term technical signals computed from `ohlcv_daily`.

Pure-compute module — reads OHLCV bars, returns a typed metrics dict.
No HTTP, no caching, no LLM. Used by the discussion context builder
to surface short-term-prediction context (1-5 trading-day horizon)
for each focus symbol so personas can ground their analysis in
quantitative signals rather than narrative-only reasoning.

Metrics (all float, all % where applicable):

  volume_ratio
      Latest day volume divided by the trailing 20-day mean
      (excluding today). > 2.0 historically marks a directional
      breakout day; > 5.0 borders speculative blow-off.

  return_5d / return_20d
      Cumulative close-to-close % return over the lookback. Used in
      tandem: positive 5d + positive 20d = sustained uptrend;
      positive 5d + negative 20d = potential reversal attempt.

  rsi_14
      Wilder's RSI over a 14-day window. Standard 30/70 thresholds
      apply (oversold / overbought). Uses simple moving average of
      gains/losses (the "first cut" Wilder formulation, not the
      smoothed exponential variant — the difference washes out at
      14 periods and the simple version is easier to verify).

  gap_pct
      (today_open - yesterday_close) / yesterday_close * 100. Useful
      with volume_ratio: a > 2% gap on > 2x volume usually signals
      news-driven repricing, not noise.

  kd_k / kd_d
      Stochastic oscillator with the Taiwan-standard period (9, 3, 3).
      `kd_k` = 100 * (close - L9) / (H9 - L9), smoothed once.
      `kd_d` = 3-period SMA of `kd_k`. Crossovers (K crossing D
      from below near 20 → bullish, from above near 80 → bearish)
      are the canonical reversal signal that complements RSI's
      momentum read.

Insufficient data: returns None when fewer than 21 bars are
available (need 20 for averages + today). Caller should drop the
symbol from the block rather than emitting partial metrics.

Industry RS (sector rotation):
  `compute_industry_rs(db, market, symbol, as_of)` returns a separate
  metrics dict comparing the symbol's last 5-day return to the median
  of every peer in the same TW industry. Output:
      industry        — e.g. "半導體業"
      peer_count      — N peers used in the median (capped at 50)
      symbol_return_5d
      industry_median_5d
      rs_score        — symbol_return_5d - industry_median_5d (% pts)
                        positive = leading the sector, negative = lagging
  Returns None when the symbol's industry isn't classified or there
  are fewer than 3 peers with usable history.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.ingest.repository import read_ohlcv_range

log = logging.getLogger(__name__)

# Need at least 21 bars: 20 for the rolling-mean window + 1 for "today".
_MIN_BARS = 21
# Read this many calendar days back to leave room for weekends + holidays
# while still hitting >= 21 trading days. ~45 days covers the worst case
# (CNY week + a one-week holiday cluster).
_LOOKBACK_DAYS = 45


@dataclass(frozen=True)
class ShortTermSignals:
    symbol: str
    as_of: str
    close: float
    volume_ratio: float | None
    return_5d: float | None
    return_20d: float | None
    rsi_14: float | None
    gap_pct: float | None
    kd_k: float | None
    kd_d: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol":       self.symbol,
            "as_of":        self.as_of,
            "close":        round(self.close, 4),
            "volume_ratio": _round_or_none(self.volume_ratio, 2),
            "return_5d":    _round_or_none(self.return_5d, 2),
            "return_20d":   _round_or_none(self.return_20d, 2),
            "rsi_14":       _round_or_none(self.rsi_14, 1),
            "gap_pct":      _round_or_none(self.gap_pct, 2),
            "kd_k":         _round_or_none(self.kd_k, 1),
            "kd_d":         _round_or_none(self.kd_d, 1),
        }


def _round_or_none(v: float | None, digits: int) -> float | None:
    return None if v is None else round(v, digits)


def _compute_rsi_14(closes: list[float]) -> float | None:
    """Wilder RSI over the last 14 closes' deltas. Needs >= 15 closes
    to produce 14 deltas."""
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(-14, 0)]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains) / 14 if gains else 0.0
    avg_loss = sum(losses) / 14 if losses else 0.0
    if avg_loss == 0:
        # All up days — formally RSI=100; capped by Wilder convention.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_kd_9_3_3(
    bars_window: list[dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Taiwan-standard 9-period stochastic with 3-period smoothing on
    both K and D. Needs at least 11 bars (9 for the look-back + 3 for
    K smoothing — the leading 8 produce no K value, the next 3 smooth
    into the first usable D).

    Implementation matches the canonical 證交所 / Yahoo formulation:
        rsv_t = (close_t - L9_t) / (H9_t - L9_t) * 100
        K_t   = (2/3) * K_{t-1} + (1/3) * rsv_t       (seed K_0 = 50)
        D_t   = (2/3) * D_{t-1} + (1/3) * K_t         (seed D_0 = 50)
    Returns the latest (K, D); (None, None) if input too short.
    """
    if len(bars_window) < 11:
        return None, None
    closes = [b.get("close") for b in bars_window]
    highs = [b.get("high") if b.get("high") is not None else b.get("close")
             for b in bars_window]
    lows = [b.get("low") if b.get("low") is not None else b.get("close")
            for b in bars_window]
    if any(c is None for c in closes[-9:]):
        return None, None

    k_prev = 50.0
    d_prev = 50.0
    k_latest: float | None = None
    d_latest: float | None = None
    # Iterate from index 8 onward (need 9 bars of look-back).
    for i in range(8, len(bars_window)):
        window_lows = [v for v in lows[i - 8:i + 1] if v is not None]
        window_highs = [v for v in highs[i - 8:i + 1] if v is not None]
        c = closes[i]
        if not window_lows or not window_highs or c is None:
            continue
        h9 = max(window_highs)
        l9 = min(window_lows)
        if h9 == l9:
            rsv = 50.0
        else:
            rsv = (c - l9) / (h9 - l9) * 100.0
        k = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * rsv
        d = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k
        k_prev, d_prev = k, d
        k_latest, d_latest = k, d
    return k_latest, d_latest


def _compute_volume_ratio(
    today_volume: int | None, prior_volumes: list[int | None],
) -> float | None:
    """Today's volume divided by the mean of the prior N volumes
    (caller provides the trailing window already excluding today).
    Returns None when today's volume is missing OR the average is
    zero (a freshly-IPO'd stock with one trading day of history)."""
    if today_volume is None:
        return None
    valid = [v for v in prior_volumes if v is not None]
    if not valid:
        return None
    mean = sum(valid) / len(valid)
    if mean <= 0:
        return None
    return today_volume / mean


async def compute_short_term_signals(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Compute the five Tier-1 short-term metrics for `symbol`. Returns
    None when:
      - the archive holds fewer than `_MIN_BARS` rows in the lookback,
      - or the most recent bar's close/volume is missing.

    `as_of=None` means "use today (UTC)" — live mode. Backtest discussions
    pass the anchor date so the metrics reflect what the personas
    *would have seen* at that point, not present-day data.
    """
    end = as_of or datetime.now(UTC).date()
    start = end - timedelta(days=_LOOKBACK_DAYS)

    try:
        bars = await read_ohlcv_range(db, market, symbol, start, end)
    except Exception as exc:
        log.warning(
            "short_term_signals.read_failed",
            extra={"symbol": symbol, "error": str(exc)},
        )
        return None

    if len(bars) < _MIN_BARS:
        return None

    # Bars are returned ascending; latest is the last entry.
    latest = bars[-1]
    prev = bars[-2]
    close = latest.get("close")
    if close is None:
        return None

    closes = [b.get("close") for b in bars if b.get("close") is not None]
    if len(closes) < _MIN_BARS:
        return None

    today_volume = latest.get("volume")
    prior_volumes = [b.get("volume") for b in bars[-21:-1]]   # 20 prior days
    volume_ratio = _compute_volume_ratio(today_volume, prior_volumes)

    # Returns: indices align with `closes` (filtered to non-null).
    return_5d: float | None = None
    return_20d: float | None = None
    if len(closes) >= 6 and closes[-6] not in (0, None):
        return_5d = (closes[-1] / closes[-6] - 1) * 100
    if len(closes) >= 21 and closes[-21] not in (0, None):
        return_20d = (closes[-1] / closes[-21] - 1) * 100

    rsi_14 = _compute_rsi_14(closes)
    kd_k, kd_d = _compute_kd_9_3_3(bars)

    # Gap: today's open vs prev close.
    today_open = latest.get("open")
    prev_close = prev.get("close")
    gap_pct: float | None = None
    if today_open is not None and prev_close not in (None, 0):
        gap_pct = (today_open - prev_close) / prev_close * 100

    signals = ShortTermSignals(
        symbol=symbol,
        as_of=str(latest.get("time") or end.isoformat()),
        close=float(close),
        volume_ratio=volume_ratio,
        return_5d=return_5d,
        return_20d=return_20d,
        rsi_14=rsi_14,
        gap_pct=gap_pct,
        kd_k=kd_k,
        kd_d=kd_d,
    )
    return signals.to_dict()


# ── Industry-relative-strength (sector rotation) ──────────────────


_INDUSTRY_RS_PEER_CAP = 50
_INDUSTRY_RS_MIN_PEERS = 3


async def compute_industry_rs(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Compare `symbol`'s 5-day return to the median 5-day return of
    every peer in the same TW industry. Returns None when:
      - market != "TW" (industry map only covers TW today),
      - the symbol's industry isn't classified,
      - fewer than `_INDUSTRY_RS_MIN_PEERS` peers have ≥ 6 bars in
        the lookback window (can't trust a 1-2-peer median).
    """
    if market != "TW":
        return None

    # Industry map lives in services.tw_market_service; lazy import
    # so this module stays standalone-testable.
    from services.tw_market_service import get_industry, get_industry_peers

    industry = get_industry(symbol)
    if industry is None:
        return None
    peers = get_industry_peers(symbol)[:_INDUSTRY_RS_PEER_CAP]
    if len(peers) < _INDUSTRY_RS_MIN_PEERS:
        return None

    # 5-day return needs 6 closes (today + 5 trading days back). Read
    # 12 calendar days back to leave room for weekends.
    end = as_of or datetime.now(UTC).date()
    start = end - timedelta(days=12)

    # The symbol's own return.
    try:
        own_bars = await read_ohlcv_range(db, market, symbol, start, end)
    except Exception as exc:
        log.warning("industry_rs.read_self_failed",
                    extra={"symbol": symbol, "error": str(exc)})
        return None
    own_return = _five_day_return(own_bars)
    if own_return is None:
        return None

    # Peer returns — bounded sequential reads. Could be batched in a
    # single SQL query later if profiling shows this dominates ctx
    # build time; today the per-symbol reads are ~1ms each so 50 peers
    # ≈ 50ms.
    peer_returns: list[float] = []
    for peer in peers:
        try:
            peer_bars = await read_ohlcv_range(db, market, peer, start, end)
        except Exception:
            continue
        r = _five_day_return(peer_bars)
        if r is not None:
            peer_returns.append(r)

    if len(peer_returns) < _INDUSTRY_RS_MIN_PEERS:
        return None

    industry_median = _median(peer_returns)
    return {
        "industry":           industry,
        "peer_count":         len(peer_returns),
        "symbol_return_5d":   round(own_return, 2),
        "industry_median_5d": round(industry_median, 2),
        "rs_score":           round(own_return - industry_median, 2),
    }


def _five_day_return(bars: list[dict[str, Any]]) -> float | None:
    """Cumulative close-to-close return over the last 5 trading days.
    Needs >= 6 bars; returns None otherwise (fresh listing, gap)."""
    closes = [b.get("close") for b in bars if b.get("close") is not None]
    if len(closes) < 6 or closes[-6] in (0, None):
        return None
    return (closes[-1] / closes[-6] - 1) * 100


def _median(values: list[float]) -> float:
    """Plain median for the small peer-return list (typically 10-50
    values). Stdlib `statistics.median` would do, but inlining
    avoids one import in a hot path."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0
