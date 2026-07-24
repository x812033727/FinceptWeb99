"""Deterministic Taiwan-stock candidate filters for daily discussions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

STRATEGIES = ("general", "chip_quality", "price_signal")
LABELS = {
    "general": "綜合選股",
    "chip_quality": "籌碼品質",
    "price_signal": "量價訊號",
}

SIGNAL_LABELS = {"breakout": "突破", "oversold": "超跌"}

# The public daily page shows each strategy's full ranked pool; general
# qualifies hundreds of symbols, so the stored snapshot is capped.
POOL_LIMIT = 50


def candidate_pool(
    ranked: list[dict[str, Any]], limit: int = POOL_LIMIT
) -> list[dict[str, Any]]:
    """Slim, capped projection of rank_candidates output for snapshots."""
    pool = []
    for row in ranked[:limit]:
        item = {"symbol": str(row["symbol"]), "strategy_score": row["strategy_score"]}
        if row.get("signal_type"):
            item["signal_type"] = row["signal_type"]
        pool.append(item)
    return pool


def _n(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = row.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return default


def _safe(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol", ""))
    return (
        symbol.isdigit()
        and len(symbol) in (4, 5, 6)
        and not row.get("is_etf")
        and not row.get("is_warrant")
        and _n(row, "close", "price") > 0
        and int(_n(row, "history_days", "bar_count")) >= 20
        and _n(row, "avg_volume_20d", "volume_ma20") >= 1_000_000
    )


def _chip_eligible(r: dict[str, Any]) -> bool:
    return (
        _n(r, "foreign_buy_days_5d") >= 3
        and _n(r, "foreign_net_buy_5d") > 0
        and _n(r, "return_5d") > 0
    )


def _quality_eligible(r: dict[str, Any]) -> bool:
    pe = _n(r, "pe", "pe_ratio", default=-1)
    return (
        _n(r, "revenue_yoy") > 0
        and _n(r, "roe") > 0
        and _n(r, "operating_cash_flow") > 0
        and 0 < pe <= 30
    )


def _breakout_eligible(r: dict[str, Any]) -> bool:
    return (
        _n(r, "close") > _n(r, "prior_high_20d", "high_20d")
        and _n(r, "volume_ratio") >= 1.5
        and _n(r, "rsi", "rsi14") <= 80
    )


def _oversold_eligible(r: dict[str, Any]) -> bool:
    return (
        _n(r, "rsi", "rsi14", default=100) <= 40
        and _n(r, "return_20d") <= -0.08
        and bool(r.get("stabilizing", _n(r, "return_1d") >= 0))
    )


def _price_signal_type(r: dict[str, Any]) -> str | None:
    # Breakout wins on the (rare) row that satisfies both tracks.
    if _breakout_eligible(r):
        return "breakout"
    if _oversold_eligible(r):
        return "oversold"
    return None


def _eligible(strategy: str, r: dict[str, Any]) -> bool:
    if strategy == "general":
        return True
    if strategy == "chip_quality":
        # Intersection may be empty on quiet days; rank_candidates -> [] and
        # candidate_batches -> [] keep that a silent no-run, not an error.
        return _chip_eligible(r) and _quality_eligible(r)
    if strategy == "price_signal":
        return _price_signal_type(r) is not None
    raise ValueError(f"unknown strategy: {strategy}")


# ── Scoring ──────────────────────────────────────────────────────
#
# Scores are the MEAN PERCENTILE RANK of a strategy's factors across
# the candidates being ranked that day, so every factor contributes on
# the same 0-1 scale.
#
# The previous design summed raw factor values with hand-tuned
# coefficients, which only works if the factors share an order of
# magnitude. They do not. Measured on real pools:
#
#   general 2618 → total 178,095, of which foreign_net_buy_5d/1000
#                  contributed 178,046 (99.97%) and return_5d*20
#                  contributed 0.74
#   general 1808 → total 164,866, of which revenue_yoy contributed
#                  162,537 (98.6%)
#
# `revenue_yoy` is a percentage that reaches five figures on a low
# base, `foreign_net_buy_5d` is a share count in the millions, and
# `return_5d` is a fraction around 0.03. Summing them means the
# largest-magnitude factor wins outright and the rest are decoration:
# `general` had degenerated into "sort by whichever of foreign buying
# or revenue growth is numerically bigger". chip_quality carried the
# same defect, and its own comment recorded the assumption that broke
# ("both component scores land in the same tens-scale ballpark" — they
# were 223,352 and 70.4).
#
# The visible symptom: 1808 with revenue_yoy = +162,537% (a low-base
# artefact) was ranked first almost every session, and the earnings
# analyst spent those sessions explaining that the number is noise.
#
# Percentile rank rather than z-score: an outlier of that size drags a
# z-score distribution so hard that every ordinary row collapses into a
# narrow band. Percentile is ordinal, so 162,537% simply lands at rank
# 1.0 for that one factor and cannot dominate the other three.
#
# Weights are equal by design. The old coefficients cannot be carried
# over — under mixed units they never expressed a relative importance
# to begin with — so equal weighting is the neutral starting point.
# These are tuning parameters: change them deliberately, with a replay
# comparing verdicts, not recommendation counts.

# factor name → (extractor, higher_is_better)
_FACTORS: dict[str, tuple[Any, bool]] = {
    "return_5d":          (lambda r: _n(r, "return_5d"), True),
    "return_20d":         (lambda r: _n(r, "return_20d"), True),
    "return_1d":          (lambda r: _n(r, "return_1d"), True),
    "foreign_net_buy_5d": (lambda r: _n(r, "foreign_net_buy_5d"), True),
    "foreign_net_buy_1d": (lambda r: _n(r, "foreign_net_buy_1d"), True),
    "foreign_buy_days_5d": (lambda r: _n(r, "foreign_buy_days_5d"), True),
    "volume_ratio":       (lambda r: _n(r, "volume_ratio"), True),
    "revenue_yoy":        (lambda r: _n(r, "revenue_yoy"), True),
    "roe":                (lambda r: _n(r, "roe"), True),
    "ocf_positive_quarters": (lambda r: _n(r, "ocf_positive_quarters"), True),
    "debt_ratio":         (lambda r: _n(r, "debt_ratio"), False),
    "pe":                 (lambda r: _n(r, "pe", default=30.0), False),
    "rsi":                (lambda r: _n(r, "rsi", "rsi14", default=100.0), False),
    "breakout_pct":       (
        lambda r: _n(
            r, "breakout_pct",
            default=(_n(r, "close") / _n(r, "prior_high_20d", default=1) - 1),
        ),
        True,
    ),
}

# Which factors each strategy averages. Deliberately the same factors
# the old formulas used, so this change is about scale only — which
# rows are *eligible* is untouched.
_STRATEGY_FACTORS: dict[str, tuple[str, ...]] = {
    "general": ("return_5d", "foreign_net_buy_5d", "revenue_yoy", "pe"),
    "chip_quality": (
        # chip half
        "foreign_buy_days_5d", "foreign_net_buy_5d", "volume_ratio", "return_5d",
        # quality half
        "revenue_yoy", "roe", "ocf_positive_quarters", "debt_ratio", "pe",
    ),
}
_SIGNAL_FACTORS: dict[str, tuple[str, ...]] = {
    "breakout": ("breakout_pct", "volume_ratio", "return_20d", "foreign_net_buy_5d"),
    "oversold": ("rsi", "return_1d", "volume_ratio", "foreign_net_buy_1d"),
}


def _percentile_ranks(values: list[float]) -> list[float]:
    """Percentile rank of each value within `values`, on 0-1.

    Ties share their average rank, so duplicate factor values (common
    when a field is missing and defaults across the pool) can't order
    rows arbitrarily. A single row scores 0.5 — with nothing to compare
    against, the neutral midpoint is the honest answer.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _factors_for(strategy: str, row: dict[str, Any]) -> tuple[str, ...]:
    if strategy == "price_signal":
        # Each track is averaged over its own factors, but both average
        # to the same 0-1 scale, so breakout and oversold rows still
        # sort against each other — the property the old per-track
        # formulas were reaching for with their shared "~10-30 scale".
        return _SIGNAL_FACTORS[str(row.get("signal_type") or "oversold")]
    return _STRATEGY_FACTORS[strategy]


def _score_pool(strategy: str, rows: list[dict[str, Any]]) -> list[float]:
    """Mean percentile rank per row, over that row's factor set.

    Each factor is ranked only among the rows that actually score on it,
    so a factor means the same thing to every row that uses it. For
    fixed-factor strategies (general, chip_quality) every row uses every
    factor, so this is the whole pool. For price_signal the two tracks
    score on different factors: a track-exclusive factor (rsi for
    oversold, breakout_pct for breakout) is ranked within its own track,
    while a shared factor (volume_ratio) still ranks across both. Pooling
    an exclusive factor over the whole pool would rank it against rows
    that carry the value but never use it — e.g. a breakout row's rsi,
    eligible up to 80 — diluting the users' percentiles.
    """
    if strategy not in _STRATEGY_FACTORS and strategy != "price_signal":
        raise ValueError(f"unknown strategy: {strategy}")
    if not rows:
        return []
    users: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        for name in _factors_for(strategy, row):
            users.setdefault(name, []).append(idx)
    pct: dict[str, dict[int, float]] = {}
    for name, idxs in users.items():
        extract, higher_is_better = _FACTORS[name]
        raw = [extract(rows[i]) for i in idxs]
        ranks = _percentile_ranks(raw)
        if not higher_is_better:
            ranks = [1.0 - x for x in ranks]
        pct[name] = dict(zip(idxs, ranks, strict=True))
    out = []
    for idx, row in enumerate(rows):
        names = _factors_for(strategy, row)
        out.append(sum(pct[name][idx] for name in names) / len(names))
    return out


def rank_candidates(strategy: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter, score and snapshot candidates; stable symbol tie-breaker."""
    eligible = []
    for source in rows:
        row = dict(source)
        if _safe(row) and _eligible(strategy, row):
            if strategy == "price_signal":
                row["signal_type"] = _price_signal_type(row)
            eligible.append(row)
    for row, score in zip(eligible, _score_pool(strategy, eligible), strict=True):
        row["strategy_score"] = round(score, 6)
    return sorted(eligible, key=lambda r: (-r["strategy_score"], str(r["symbol"])))


def candidate_batches(
    ranked: list[dict[str, Any]], run_count: int, size: int = 5
) -> list[list[dict[str, Any]]]:
    """Return up to run_count non-overlapping batches; never pad weak stocks."""
    return [ranked[i : i + size] for i in range(0, min(len(ranked), run_count * size), size)]


def _candidate_display(strategy: str, item: dict[str, Any]) -> str:
    symbol = str(item["symbol"])
    if strategy == "price_signal":
        label = SIGNAL_LABELS.get(str(item.get("signal_type")))
        if label:
            return f"{symbol}（{label}）"
    return symbol


def build_topic(strategy: str, batch: list[dict[str, Any]], general_topic: str) -> str:
    symbols = "、".join(_candidate_display(strategy, item) for item in batch)
    if strategy == "general":
        return f"{general_topic}\n本場候選股：{symbols}。僅能從候選股推薦 1–3 檔。"
    return f"{LABELS[strategy]}策略候選股：{symbols}。請完成五輪討論，僅從候選股推薦 1–3 檔並說明進出場與風險。"
