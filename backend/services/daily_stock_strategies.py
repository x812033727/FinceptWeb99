"""Deterministic Taiwan-stock candidate filters for daily discussions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

STRATEGIES = ("general", "chip_momentum", "quality_growth", "breakout", "oversold_reversal")
LABELS = {
    "general": "綜合選股",
    "chip_momentum": "籌碼動能",
    "quality_growth": "品質成長",
    "breakout": "突破追價",
    "oversold_reversal": "超跌反轉",
}


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


def _eligible(strategy: str, r: dict[str, Any]) -> bool:
    if strategy == "general":
        return True
    if strategy == "chip_momentum":
        return (
            _n(r, "foreign_buy_days_5d") >= 3
            and _n(r, "foreign_net_buy_5d") > 0
            and _n(r, "return_5d") > 0
        )
    if strategy == "quality_growth":
        pe = _n(r, "pe", "pe_ratio", default=-1)
        return (
            _n(r, "revenue_yoy") > 0
            and _n(r, "roe") > 0
            and _n(r, "operating_cash_flow") > 0
            and 0 < pe <= 30
        )
    if strategy == "breakout":
        return (
            _n(r, "close") > _n(r, "prior_high_20d", "high_20d")
            and _n(r, "volume_ratio") >= 1.5
            and _n(r, "rsi", "rsi14") <= 80
        )
    if strategy == "oversold_reversal":
        return (
            _n(r, "rsi", "rsi14", default=100) <= 40
            and _n(r, "return_20d") <= -0.08
            and bool(r.get("stabilizing", _n(r, "return_1d") >= 0))
        )
    raise ValueError(f"unknown strategy: {strategy}")


def _score(strategy: str, r: dict[str, Any]) -> float:
    if strategy == "general":
        return (
            _n(r, "return_5d") * 20
            + _n(r, "foreign_net_buy_5d") / 1000
            + _n(r, "revenue_yoy")
            + max(0, 30 - _n(r, "pe", default=30))
        )
    if strategy == "chip_momentum":
        return (
            _n(r, "foreign_buy_days_5d") * 10
            + _n(r, "foreign_net_buy_5d") / 1000
            + _n(r, "volume_ratio") * 4
            + _n(r, "return_5d") * 100
        )
    if strategy == "quality_growth":
        return (
            _n(r, "revenue_yoy")
            + _n(r, "roe")
            + _n(r, "ocf_positive_quarters") * 3
            - _n(r, "debt_ratio") / 5
            + (30 - _n(r, "pe"))
        )
    if strategy == "breakout":
        return (
            _n(r, "breakout_pct", default=(_n(r, "close") / _n(r, "prior_high_20d", default=1) - 1))
            * 100
            + _n(r, "volume_ratio") * 5
            + _n(r, "return_20d") * 50
            + _n(r, "foreign_net_buy_5d") / 2000
        )
    return (
        (40 - _n(r, "rsi", "rsi14"))
        + _n(r, "return_1d") * 100
        + _n(r, "volume_ratio") * 3
        + _n(r, "foreign_net_buy_1d") / 1000
    )


def rank_candidates(strategy: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter, score and snapshot candidates; stable symbol tie-breaker."""
    ranked = []
    for source in rows:
        row = dict(source)
        if _safe(row) and _eligible(strategy, row):
            row["strategy_score"] = round(_score(strategy, row), 6)
            ranked.append(row)
    return sorted(ranked, key=lambda r: (-r["strategy_score"], str(r["symbol"])))


def candidate_batches(
    ranked: list[dict[str, Any]], run_count: int, size: int = 5
) -> list[list[dict[str, Any]]]:
    """Return up to run_count non-overlapping batches; never pad weak stocks."""
    return [ranked[i : i + size] for i in range(0, min(len(ranked), run_count * size), size)]


def build_topic(strategy: str, batch: list[dict[str, Any]], general_topic: str) -> str:
    symbols = "、".join(str(item["symbol"]) for item in batch)
    if strategy == "general":
        return f"{general_topic}\n本場候選股：{symbols}。僅能從候選股推薦 1–3 檔。"
    return f"{LABELS[strategy]}策略候選股：{symbols}。請完成五輪討論，僅從候選股推薦 1–3 檔並說明進出場與風險。"
