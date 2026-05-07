"""PR-2: tests for `compute_conclusion_diff` — the structured delta
between an original conclusion and a post-mortem revised one.
"""
from __future__ import annotations

from typing import Any

from services.discussion_service import compute_conclusion_diff


def _conclusion(
    recs: list[tuple[str, float]] | None = None,
    *,
    reasoning: str = "",
    consensus: float = 0.5,
    horizon: str = "short_term",
) -> dict[str, Any]:
    return {
        "recommended_symbols": [s for s, _ in (recs or [])],
        "recommendations": [
            {"symbol": s, "confidence": c} for s, c in (recs or [])
        ],
        "reasoning": reasoning,
        "risks": [],
        "time_horizon": horizon,
        "consensus_score": consensus,
    }


# ── happy path ────────────────────────────────────────────────────


def test_diff_added_and_removed_symbols():
    orig = _conclusion([("2330", 0.7), ("2454", 0.6)])
    post = _conclusion([("2330", 0.7), ("2317", 0.5)])
    d = compute_conclusion_diff(orig, post)
    assert d is not None
    assert d["symbols_added"] == ["2317"]
    assert d["symbols_removed"] == ["2454"]


def test_diff_confidence_changes_filtered_below_005_noise_floor():
    """Only flag confidence shifts that exceed the 0.05 floor — a
    rounding-noise change shouldn't surface as a 'change'."""
    orig = _conclusion([("2330", 0.70), ("2454", 0.60)])
    post = _conclusion([("2330", 0.71), ("2454", 0.30)])
    d = compute_conclusion_diff(orig, post)
    assert d is not None
    # 2330 shifted by 0.01 → suppressed
    assert "2330" not in d["confidence_changes"]
    # 2454 shifted by 0.30 → surfaced
    assert d["confidence_changes"]["2454"] == {
        "orig": 0.6, "post": 0.3, "delta": -0.3,
    }


def test_diff_consensus_and_horizon_changes():
    orig = _conclusion([("2330", 0.7)], consensus=0.8, horizon="short_term")
    post = _conclusion([("2330", 0.7)], consensus=0.5, horizon="long_term")
    d = compute_conclusion_diff(orig, post)
    assert d is not None
    assert d["consensus_score_delta"] == -0.3
    assert d["time_horizon_changed"] is True


def test_diff_reasoning_overlap_jaccard():
    """Identical reasoning → 1.0 overlap; disjoint → 0.0."""
    same = compute_conclusion_diff(
        _conclusion(reasoning="外資轉多 半導體領漲 風險偏好回升"),
        _conclusion(reasoning="外資轉多 半導體領漲 風險偏好回升"),
    )
    assert same is not None
    assert same["reasoning_overlap"] == 1.0

    disjoint = compute_conclusion_diff(
        _conclusion(reasoning="aaaa bbbb cccc"),
        _conclusion(reasoning="dddd eeee ffff"),
    )
    assert disjoint is not None
    assert disjoint["reasoning_overlap"] == 0.0

    partial = compute_conclusion_diff(
        _conclusion(reasoning="aaaa bbbb cccc"),
        _conclusion(reasoning="aaaa bbbb dddd"),
    )
    assert partial is not None
    # |A∩B|=2 (aaaa, bbbb), |A∪B|=4 → 0.5
    assert partial["reasoning_overlap"] == 0.5


# ── degenerate inputs ────────────────────────────────────────────


def test_diff_returns_none_on_parse_error_either_side():
    err = {"_parse_error": True, "recommendations": []}
    ok = _conclusion([("2330", 0.7)])
    assert compute_conclusion_diff(err, ok) is None
    assert compute_conclusion_diff(ok, err) is None
    assert compute_conclusion_diff(err, err) is None


def test_diff_returns_none_when_either_side_is_none_or_non_dict():
    assert compute_conclusion_diff(None, _conclusion()) is None
    assert compute_conclusion_diff(_conclusion(), None) is None
    assert compute_conclusion_diff("not a dict", _conclusion()) is None  # type: ignore[arg-type]


def test_diff_handles_empty_conclusions():
    """Two empty conclusions = no symbols, no shifts, full overlap on
    empty reasoning. Should produce a meaningful (but trivial) diff."""
    d = compute_conclusion_diff(_conclusion(), _conclusion())
    assert d is not None
    assert d["symbols_added"] == []
    assert d["symbols_removed"] == []
    assert d["confidence_changes"] == {}
    assert d["consensus_score_delta"] == 0.0
    assert d["time_horizon_changed"] is False
    assert d["reasoning_overlap"] == 1.0


def test_diff_handles_bad_confidence_values():
    """Strings / NaN / None confidence in input → fallback to 0.5
    in the diff math (matches `_parse_recommendations` behavior)."""
    orig = {
        "recommendations": [
            {"symbol": "2330", "confidence": "bad"},
            {"symbol": "2454", "confidence": None},
        ],
        "reasoning": "",
        "consensus_score": 0.5,
        "time_horizon": "short_term",
    }
    post = {
        "recommendations": [
            {"symbol": "2330", "confidence": 0.9},  # 0.5 → 0.9 = 0.4 delta
            {"symbol": "2454", "confidence": 0.5},  # 0.5 → 0.5 = 0
        ],
        "reasoning": "",
        "consensus_score": 0.5,
        "time_horizon": "short_term",
    }
    d = compute_conclusion_diff(orig, post)
    assert d is not None
    assert "2330" in d["confidence_changes"]
    assert d["confidence_changes"]["2330"]["delta"] == 0.4
    assert "2454" not in d["confidence_changes"]
