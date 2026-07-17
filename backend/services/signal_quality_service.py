"""Empirical signal-quality validation against D1-D5 outcomes.

The audit tooling (PRs #239-#240) tells us **whether** personas
cite each signal. This service answers the harder question:
**does the signal actually predict moves?**

For every concluded discussion older than 7 days that has
``daily_close_prices`` populated, we:

  1. Look up each round-1 context snapshot.
  2. Extract a curated list of signal values for each symbol the
     scoreboard covers.
  3. Pair the signal value with the symbol's D5 return computed
     from ``day1_open_prices`` × ``daily_close_prices``.

Then for each signal we aggregate across discussions:

  - Numeric signals (RSI, volume_ratio, return_5d, …) → Pearson
    correlation between signal value and D5 return + sample size.
  - Categorical signals (taifex.trend, day_trading_trend.trend,
    securities_lending_trend.trend) → mean D5 return per category +
    n per category.

Read-only. Never writes. Designed to run hot against production —
the heaviest path is one SELECT for round contexts + one SELECT
for discussions in the lookback window, both already indexed.

Why per-symbol D5 vs per-discussion verdict: the verdict_reason
column only carries a thumbs-up/down signal (PR #169 verifier)
which is too coarse for correlation work. D5 return is continuous
and standardised (% from day-1 open).
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from models.discussion import Discussion
from models.discussion_round_context import DiscussionRoundContext

log = logging.getLogger(__name__)


# Curated signal taxonomy. `path` uses `{symbol}` as a placeholder
# for the per-symbol substitution; resolved against the round-1
# context blob via `_extract_signal_value`. `kind` drives the
# aggregator: `numeric` runs Pearson, `categorical` runs per-bucket
# means.
@dataclass(frozen=True)
class SignalSpec:
    path: str
    kind: str   # "numeric" | "categorical"
    label: str   # human-readable column header


_SIGNAL_TAXONOMY: tuple[SignalSpec, ...] = (
    # Numeric — per-symbol short-term technicals
    SignalSpec("short_term_signals.{symbol}.rsi_14",         "numeric", "RSI(14)"),
    SignalSpec("short_term_signals.{symbol}.volume_ratio",   "numeric", "Volume ratio"),
    SignalSpec("short_term_signals.{symbol}.return_5d",      "numeric", "Return 5d %"),
    SignalSpec("short_term_signals.{symbol}.return_20d",     "numeric", "Return 20d %"),
    SignalSpec("short_term_signals.{symbol}.kd_k",           "numeric", "KD %K"),
    SignalSpec("short_term_signals.{symbol}.gap_pct",        "numeric", "Gap %"),
    SignalSpec("short_term_signals.{symbol}.macd_hist",      "numeric", "MACD histogram"),
    SignalSpec(
        "short_term_signals.{symbol}.bollinger_pct_b",
        "numeric", "Bollinger %B",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.obv_5d_change_pct",
        "numeric", "OBV 5d Δ%",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.holdings_concentration_trend.change_pp",
        "numeric", "Top-holders Δpp (4w)",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.industry_rs.rs_score",
        "numeric", "Industry RS",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.day_trading_trend.latest_ratio",
        "numeric", "Day-trading ratio",
    ),
    # Numeric — market-wide
    SignalSpec(
        "taifex_positioning.fini.change_5d",
        "numeric", "TAIFEX 外資 OI Δ5d",
    ),
    # Categorical — per-symbol
    SignalSpec(
        "short_term_signals.{symbol}.day_trading_trend.trend",
        "categorical", "Day-trading trend",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.securities_lending_trend.trend",
        "categorical", "SBL trend",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.upcoming_event.next_event",
        "categorical", "Upcoming event",
    ),
    SignalSpec(
        "short_term_signals.{symbol}.holdings_concentration_trend.trend",
        "categorical", "Top-holders trend",
    ),
    # Categorical — market-wide
    SignalSpec("taifex_positioning.trend",   "categorical", "TAIFEX trend"),
)


@dataclass
class NumericStats:
    label: str
    path: str
    n: int
    mean_value: float
    mean_d5_return: float
    pearson_r: float | None
    # Sign-match: % of pairs where (signal > mean) ↔ (d5 > 0). Crude
    # but useful when correlation is weak — tells you if the signal
    # at least gets DIRECTION right even if magnitude is noisy.
    sign_match_pct: float | None


@dataclass
class CategoricalBucket:
    n: int
    mean_d5_return: float
    median_d5_return: float


@dataclass
class CategoricalStats:
    label: str
    path: str
    n_total: int
    by_category: dict[str, CategoricalBucket]


@dataclass
class SignalQualityReport:
    """Top-level result returned by ``compute_signal_quality``."""
    discussions_audited: int
    discussion_ids: list[str]
    lookback_days: int
    market: str | None
    numeric: list[NumericStats] = field(default_factory=list)
    categorical: list[CategoricalStats] = field(default_factory=list)


# ── Path extractor ────────────────────────────────────────────────


def _extract_signal_value(
    ctx: dict[str, Any], path: str, symbol: str,
) -> Any:
    """Walk a dotted path against `ctx`, substituting `{symbol}` for
    the per-symbol slot. Returns None for any missing intermediate
    step (so the caller treats "signal absent in ctx" identically to
    "ctx column was None")."""
    parts = path.split(".")
    cur: Any = ctx
    for p in parts:
        if p == "{symbol}":
            p = symbol
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
        if cur is None:
            return None
    return cur


# ── D5 return extractor ───────────────────────────────────────────


def _d5_return(
    day1_opens: dict[str, float] | None,
    daily_closes: dict[str, list[float | None]] | None,
    symbol: str,
) -> float | None:
    """Compute D5 return = (D5 close − day1 open) / day1 open × 100.

    Returns None when:
      - day1 open is missing / zero,
      - the daily_closes list has no D5 entry (still resolving), or
      - both columns are NULL (cron hasn't run yet on this discussion).
    """
    if not day1_opens or not daily_closes:
        return None
    open_ = day1_opens.get(symbol)
    closes = daily_closes.get(symbol) or []
    if open_ is None or open_ == 0:
        return None
    if len(closes) < 5:
        return None
    d5 = closes[4]
    if d5 is None:
        return None
    return (float(d5) / float(open_) - 1) * 100.0


# ── Aggregators ───────────────────────────────────────────────────


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Standard Pearson correlation coefficient. Returns None when
    n < 3 or either series has zero variance (`r` is undefined)."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return num / math.sqrt(var_x * var_y)


def _aggregate_numeric(
    spec: SignalSpec,
    pairs: list[tuple[float, float]],   # (signal_value, d5_return)
) -> NumericStats:
    if not pairs:
        return NumericStats(
            label=spec.label, path=spec.path, n=0,
            mean_value=0.0, mean_d5_return=0.0,
            pearson_r=None, sign_match_pct=None,
        )
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    r = _pearson(xs, ys)
    # Sign-match: signal above its own mean ↔ d5 > 0.
    matches = sum(
        1 for x, y in pairs if (x > mean_x) == (y > 0)
    )
    sign_match = (matches / len(pairs)) * 100.0
    return NumericStats(
        label=spec.label, path=spec.path, n=len(pairs),
        mean_value=round(mean_x, 4),
        mean_d5_return=round(mean_y, 4),
        pearson_r=round(r, 4) if r is not None else None,
        sign_match_pct=round(sign_match, 2),
    )


def _aggregate_categorical(
    spec: SignalSpec,
    pairs: list[tuple[str, float]],   # (category, d5_return)
) -> CategoricalStats:
    by_cat: dict[str, list[float]] = {}
    for cat, d5 in pairs:
        by_cat.setdefault(cat, []).append(d5)
    out: dict[str, CategoricalBucket] = {}
    for cat, d5s in by_cat.items():
        out[cat] = CategoricalBucket(
            n=len(d5s),
            mean_d5_return=round(statistics.mean(d5s), 4),
            median_d5_return=round(statistics.median(d5s), 4),
        )
    return CategoricalStats(
        label=spec.label, path=spec.path,
        n_total=len(pairs), by_category=out,
    )


# ── DB-bound entry point ──────────────────────────────────────────


async def compute_signal_quality(
    db: AsyncSession,
    *,
    lookback_days: int = 60,
    market: str | None = None,
    status: str = "concluded",
) -> SignalQualityReport:
    """Roll up signal-vs-D5 statistics across discussions concluded
    in the lookback window. Discussions without persisted round
    contexts OR without D5 data are silently skipped (the cron
    reaches them eventually; this report is best-effort over what's
    already resolved).

    `market` filters discussion rows by market when set; default is
    all-markets. `status` defaults to `concluded` — that's the only
    state where the daily_close_prices cron has fired.
    """
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    # The audit only reads id + the two price maps; don't materialize
    # the rest of the row's JSON columns.
    stmt = (
        select(Discussion)
        .options(load_only(
            Discussion.id,
            Discussion.day1_open_prices,
            Discussion.daily_close_prices,
        ))
        .where(
            Discussion.status == status,
            Discussion.created_at >= cutoff,
            Discussion.daily_close_prices.isnot(None),
            Discussion.day1_open_prices.isnot(None),
        )
        .order_by(Discussion.created_at.desc())
    )
    if market is not None:
        stmt = stmt.where(Discussion.market == market)

    discussions = (await db.scalars(stmt)).all()
    if not discussions:
        return SignalQualityReport(
            discussions_audited=0, discussion_ids=[],
            lookback_days=lookback_days, market=market,
        )

    # Index round contexts by discussion_id for round-1 lookup.
    disc_ids = [d.id for d in discussions]
    ctx_stmt = (
        select(DiscussionRoundContext)
        .where(
            DiscussionRoundContext.discussion_id.in_(disc_ids),
            DiscussionRoundContext.round == 1,
        )
    )
    contexts = {
        row.discussion_id: row.context
        for row in (await db.scalars(ctx_stmt)).all()
    }

    # Bucket pairs per signal.
    numeric_pairs: dict[str, list[tuple[float, float]]] = {
        spec.path: [] for spec in _SIGNAL_TAXONOMY if spec.kind == "numeric"
    }
    categorical_pairs: dict[str, list[tuple[str, float]]] = {
        spec.path: [] for spec in _SIGNAL_TAXONOMY if spec.kind == "categorical"
    }

    audited_ids: list[str] = []
    for disc in discussions:
        ctx = contexts.get(disc.id)
        if ctx is None:
            continue
        # Iterate every symbol that has a D5 close — gives more
        # samples than restricting to recommended_symbols, since
        # focus_symbols / scoreboard backfill cover broader sets.
        symbols = list((disc.daily_close_prices or {}).keys())
        if not symbols:
            continue
        had_any = False
        for symbol in symbols:
            d5 = _d5_return(
                disc.day1_open_prices,
                disc.daily_close_prices,
                symbol,
            )
            if d5 is None:
                continue
            for spec in _SIGNAL_TAXONOMY:
                value = _extract_signal_value(ctx, spec.path, symbol)
                if value is None:
                    continue
                if spec.kind == "numeric":
                    try:
                        v = float(value)
                    except (TypeError, ValueError):
                        continue
                    numeric_pairs[spec.path].append((v, d5))
                else:
                    categorical_pairs[spec.path].append((str(value), d5))
                had_any = True
        if had_any:
            audited_ids.append(str(disc.id))

    numeric_stats = [
        _aggregate_numeric(spec, numeric_pairs[spec.path])
        for spec in _SIGNAL_TAXONOMY
        if spec.kind == "numeric"
    ]
    categorical_stats = [
        _aggregate_categorical(spec, categorical_pairs[spec.path])
        for spec in _SIGNAL_TAXONOMY
        if spec.kind == "categorical"
    ]

    return SignalQualityReport(
        discussions_audited=len(audited_ids),
        discussion_ids=audited_ids,
        lookback_days=lookback_days,
        market=market,
        numeric=numeric_stats,
        categorical=categorical_stats,
    )
