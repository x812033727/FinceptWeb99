"""Unit tests for the pure helpers in `scripts.audit_block_efficiency`.

The DB-reading path (`_collect_block_bytes`, `_run`) is exercised only
manually against real data; here we pin the byte↔citation join logic:
block pooling of fan-out signals, byte-share math, and the trim verdict.
"""
from __future__ import annotations

from scripts.audit_block_efficiency import (
    _build_stats,
    _pool_citation_by_block,
)


def test_pool_folds_fanout_signals_into_their_block():
    coverage = {
        "short_term_signals.rsi_14": {
            "present": 5, "cited": 2, "cited_with_value": 1, "persona_count": 10,
        },
        "short_term_signals.volume_ratio": {
            "present": 5, "cited": 1, "cited_with_value": 0, "persona_count": 10,
        },
        "taifex_positioning": {
            "present": 3, "cited": 3, "cited_with_value": 2, "persona_count": 6,
        },
    }
    pooled = _pool_citation_by_block(coverage)
    # Two sub-signals fold into one block, summing counts.
    assert pooled["short_term_signals"]["cited"] == 3
    assert pooled["short_term_signals"]["persona_count"] == 20
    assert pooled["short_term_signals"]["signals"] == 2
    # 1:1 signal pools under itself.
    assert pooled["taifex_positioning"]["cited"] == 3
    assert pooled["taifex_positioning"]["signals"] == 1


def test_never_cited_heavy_block_is_trim_candidate():
    per_block = {
        # heavy bytes, present but never cited → TRIM
        "focus_briefs": [8000, 8000, 8000],
        # small, well cited → ok
        "taifex_positioning": [200, 200],
    }
    pooled = {
        "focus_briefs": {
            "cited": 0, "cited_with_value": 0, "persona_count": 9, "signals": 1,
        },
        "taifex_positioning": {
            "cited": 2, "cited_with_value": 2, "persona_count": 2, "signals": 1,
        },
    }
    stats = {s.block: s for s in _build_stats(per_block, pooled)}
    assert stats["focus_briefs"].verdict == "TRIM: never cited"
    assert stats["focus_briefs"].cited_rate == 0.0
    # Heaviest-first ordering: focus_briefs (24000B) before taifex (400B).
    ordered = _build_stats(per_block, pooled)
    assert ordered[0].block == "focus_briefs"
    assert stats["taifex_positioning"].verdict == "ok"


def test_unaudited_block_reports_none_citation():
    per_block = {"recent_lessons": [1000, 1000]}
    stats = _build_stats(per_block, {})  # no coverage for this block
    s = stats[0]
    assert s.cited_rate is None
    assert s.value_rate is None
    assert s.verdict.startswith("not-audited")


def test_metadata_block_never_flagged_for_trim():
    per_block = {"errors": [5000, 5000, 5000]}  # heavy but structural
    stats = _build_stats(per_block, {})
    assert stats[0].verdict == "metadata"


def test_byte_share_sums_to_100():
    per_block = {"a": [100], "b": [300]}
    stats = _build_stats(per_block, {})
    total = sum(s.byte_share_pct for s in stats)
    assert abs(total - 100.0) < 0.01
