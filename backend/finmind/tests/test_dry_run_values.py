"""Tests for the `--values` value-level comparison in
`finmind.scripts.dry_run_cutover`.

All pure functions — no network, no DB. Covers the cell-comparison
tolerance kinds, the join/coverage/mismatch aggregation, the WARN/OK
status thresholds, and the compare_spec lookup wiring.
"""
from __future__ import annotations

from finmind.ingest.mappings._types import CompareSpec
from finmind.scripts.dry_run_cutover import (
    ValueDiff,
    _resolve_compare_spec,
    _values_match,
    compare_values,
)


# ── _values_match: per-cell tolerance kinds ──────────────────────


def test_values_match_rel_within_and_outside_tolerance():
    assert _values_match(100.0, 100.4, "rel", 0.005) is True   # 0.4%
    assert _values_match(100.0, 101.0, "rel", 0.005) is False  # 1.0%


def test_values_match_abs():
    assert _values_match(1000, 1000, "abs", 0.0) is True
    assert _values_match(1000, 1001, "abs", 0.0) is False


def test_values_match_exact_strings():
    assert _values_match(" 處置 ", "處置", "exact", 0.0) is True
    assert _values_match("A", "B", "exact", 0.0) is False


def test_values_match_both_empty_is_incomparable():
    assert _values_match("", "", "rel", 0.005) is None
    assert _values_match(None, None, "exact", 0.0) is None


def test_values_match_one_missing_is_mismatch():
    assert _values_match(5.0, "", "rel", 0.005) is False


def test_values_match_both_zero_is_match():
    assert _values_match(0, 0, "rel", 0.005) is True


# ── compare_values: join + aggregation ───────────────────────────


_SPEC = CompareSpec(
    key_cols=("date", "stock_id"),
    value_cols=(("close", "rel", 0.005),),
)


def test_compare_values_all_match():
    fm = [{"date": "2026-01-02", "stock_id": "2330", "close": 600.0}]
    sc = [{"date": "2026-01-02", "stock_id": "2330", "close": 600.5}]
    diff = compare_values("TaiwanStockPrice", "twse", fm, sc, _SPEC)
    assert diff.coverage_pct == 100.0
    assert diff.worst_mismatch_pct() == 0.0
    assert diff.status(98.0, 1.0) == "OK"


def test_compare_values_detects_value_mismatch():
    fm = [{"date": "2026-01-02", "stock_id": "2330", "close": 600.0}]
    sc = [{"date": "2026-01-02", "stock_id": "2330", "close": 700.0}]
    diff = compare_values("TaiwanStockPrice", "twse", fm, sc, _SPEC)
    assert diff.per_col["close"] == (1, 1)
    assert diff.worst_mismatch_pct() == 100.0
    assert diff.status(98.0, 1.0) == "WARN"
    assert diff.samples  # a sample line was captured


def test_compare_values_partial_coverage():
    fm = [
        {"date": "2026-01-02", "stock_id": "2330", "close": 600.0},
        {"date": "2026-01-02", "stock_id": "2317", "close": 100.0},
    ]
    sc = [{"date": "2026-01-02", "stock_id": "2330", "close": 600.0}]
    diff = compare_values("TaiwanStockPrice", "twse", fm, sc, _SPEC)
    assert diff.finmind_keys == 2
    assert diff.common_keys == 1
    assert diff.coverage_pct == 50.0
    assert diff.status(98.0, 1.0) == "WARN"  # coverage below floor


def test_compare_values_ignores_incomparable_pairs_in_denominator():
    """A key present on both sides but with both close values empty
    contributes to coverage but NOT to the mismatch denominator."""
    fm = [{"date": "2026-01-02", "stock_id": "2330", "close": ""}]
    sc = [{"date": "2026-01-02", "stock_id": "2330", "close": ""}]
    diff = compare_values("TaiwanStockPrice", "twse", fm, sc, _SPEC)
    assert diff.common_keys == 1
    assert diff.per_col["close"] == (0, 0)  # nothing comparable
    assert diff.status(98.0, 1.0) == "OK"


# ── compare_spec registry wiring ─────────────────────────────────


def test_resolve_compare_spec_present_for_price():
    spec = _resolve_compare_spec("TaiwanStockPrice")
    assert spec is not None
    assert spec.key_cols == ("date", "stock_id")
    assert any(col == "close" for col, _, _ in spec.value_cols)


def test_resolve_compare_spec_absent_returns_none():
    # A dataset with no compare_spec authored yet.
    assert _resolve_compare_spec("TaiwanStockInfo") is None


def test_value_diff_status_fail_on_error():
    d = ValueDiff(
        dataset="X", source="twse", finmind_keys=0, selfcrawl_keys=0,
        common_keys=0, per_col={}, samples=[], error="boom",
    )
    assert d.status(98.0, 1.0) == "FAIL"
