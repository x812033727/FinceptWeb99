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


def _chip_score(r: dict[str, Any]) -> float:
    return (
        _n(r, "foreign_buy_days_5d") * 10
        + _n(r, "foreign_net_buy_5d") / 1000
        + _n(r, "volume_ratio") * 4
        + _n(r, "return_5d") * 100
    )


def _quality_score(r: dict[str, Any]) -> float:
    return (
        _n(r, "revenue_yoy")
        + _n(r, "roe")
        + _n(r, "ocf_positive_quarters") * 3
        - _n(r, "debt_ratio") / 5
        + (30 - _n(r, "pe"))
    )


def _breakout_score(r: dict[str, Any]) -> float:
    return (
        _n(r, "breakout_pct", default=(_n(r, "close") / _n(r, "prior_high_20d", default=1) - 1))
        * 100
        + _n(r, "volume_ratio") * 5
        + _n(r, "return_20d") * 50
        + _n(r, "foreign_net_buy_5d") / 2000
    )


def _oversold_score(r: dict[str, Any]) -> float:
    return (
        (40 - _n(r, "rsi", "rsi14"))
        + _n(r, "return_1d") * 100
        + _n(r, "volume_ratio") * 3
        + _n(r, "foreign_net_buy_1d") / 1000
    )


def _score(strategy: str, r: dict[str, Any]) -> float:
    if strategy == "general":
        return (
            _n(r, "return_5d") * 20
            + _n(r, "foreign_net_buy_5d") / 1000
            + _n(r, "revenue_yoy")
            + max(0, 30 - _n(r, "pe", default=30))
        )
    if strategy == "chip_quality":
        # Both component scores land in the same tens-scale ballpark, so a
        # plain 50/50 blend ranks "strong on both" rows first without
        # cross-row normalization.
        return 0.5 * _chip_score(r) + 0.5 * _quality_score(r)
    if strategy == "price_signal":
        # Each track keeps its own formula; both are built on the same
        # ~10-30 scale so raw scores sort together.
        if _price_signal_type(r) == "breakout":
            return _breakout_score(r)
        return _oversold_score(r)
    raise ValueError(f"unknown strategy: {strategy}")


def rank_candidates(strategy: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter, score and snapshot candidates; stable symbol tie-breaker."""
    ranked = []
    for source in rows:
        row = dict(source)
        if _safe(row) and _eligible(strategy, row):
            if strategy == "price_signal":
                row["signal_type"] = _price_signal_type(row)
            row["strategy_score"] = round(_score(strategy, row), 6)
            ranked.append(row)
    return sorted(ranked, key=lambda r: (-r["strategy_score"], str(r["symbol"])))


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
