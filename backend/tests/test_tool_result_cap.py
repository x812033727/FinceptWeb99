"""Unit tests for `_cap_tool_result` — the tool-loop result truncator
that keeps oversized tool outputs from being re-sent verbatim on every
subsequent tool iteration (the dominant driver of MiniMax prompt-token
peaks). See ai/llm_router._cap_tool_result.
"""
from __future__ import annotations

import json

from ai.llm_router import _cap_tool_result


def _big_institutional(n: int = 90) -> str:
    rows = [
        {"date": f"2026-01-{(i % 28) + 1:02d}", "foreign": 1000 + i,
         "trust": -200 + i, "dealer": 50 - i}
        for i in range(n)
    ]
    return json.dumps(
        {"symbol": "2330", "days": n, "count": n, "rows": rows},
        ensure_ascii=False,
    )


def test_small_result_passes_through_untouched():
    small = json.dumps({"symbol": "2330", "price": 1000, "count": 1,
                        "rows": [{"date": "2026-01-29", "foreign": 1}]})
    assert _cap_tool_result("get_institutional_history", small, 12_000) == small


def test_large_list_kept_as_head_tail_with_count():
    big = _big_institutional(90)
    assert len(big) > 100  # sanity
    capped = _cap_tool_result("get_institutional_history", big, limit=1_500)

    # still valid JSON
    data = json.loads(capped)
    # summary fields preserved
    assert data["symbol"] == "2330"
    assert data["count"] == 90
    # list reduced to head15 + tail5, original count recorded
    assert len(data["rows"]) == 20
    assert data["_truncated"]["original_counts"]["rows"] == 90
    # head + tail are the real first / last rows (recency + earliest kept)
    assert data["rows"][0]["date"] == "2026-01-01"
    assert data["rows"][-1]["foreign"] == 1000 + 89
    # actually smaller than the original
    assert len(capped) < len(big)


def test_error_path_is_caller_guarded_not_here():
    # _cap_tool_result itself is size-gated; an error payload is small so
    # it passes through. (The loop additionally skips capping when
    # is_error, kept full for diagnostics.)
    err = json.dumps({"error": "FinMind quota exceeded"})
    assert _cap_tool_result("get_margin_history", err, 12_000) == err


def test_non_json_oversized_falls_back_to_char_slice():
    blob = "x" * 50_000
    capped = _cap_tool_result("weird_tool", blob, limit=2_000)
    assert len(capped) < len(blob)
    assert "truncated" in capped
    # head + tail of the original retained
    assert capped.startswith("x")
    assert capped.endswith("x")


def test_json_without_known_list_key_falls_back_but_smaller():
    # A dict whose big payload is under a non-whitelisted key still gets
    # shrunk via the char-slice fallback rather than left huge.
    blob = json.dumps({"symbol": "2330", "weird_blob": ["y" * 100 for _ in range(500)]})
    capped = _cap_tool_result("run_dcf", blob, limit=3_000)
    assert len(capped) <= 3_000 + 200
    assert "truncated" in capped
