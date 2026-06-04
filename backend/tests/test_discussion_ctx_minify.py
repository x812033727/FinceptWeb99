"""Unit tests for the lossless ctx minifier + prompt-size breakdown.

Both are pure helpers (no DB / LLM / Redis), so these run without the
`client` / Redis fixtures.
"""
from __future__ import annotations

import json

from services.discussion.ctx_minify import _minify_for_prompt
from services.discussion.usage_breakdown import measure_prompt_breakdown


def test_minify_drops_none_dict_values():
    src = {"a": 1, "b": None, "nested": {"x": None, "y": 2}}
    assert _minify_for_prompt(src) == {"a": 1, "nested": {"y": 2}}


def test_minify_preserves_false_zero_and_empty():
    # Booleans / zeros / empty collections carry meaning — never dropped.
    src = {
        "flag": False,
        "zero": 0,
        "fzero": 0.0,
        "empty_str": "",
        "empty_list": [],
        "empty_dict": {},
    }
    assert _minify_for_prompt(src) == src


def test_minify_collapses_float_noise_to_6_sig_figs():
    out = _minify_for_prompt({"price": 650.0000000001, "ratio": 1.23456789})
    assert out["price"] == 650.0
    assert out["ratio"] == 1.23457


def test_minify_keeps_small_magnitudes():
    # Significant *figures*, not decimal places — small values survive.
    out = _minify_for_prompt({"tiny": 0.000123456789})
    assert out["tiny"] == 0.000123457
    assert out["tiny"] != 0.0


def test_minify_drops_nan_and_inf_keys_and_output_is_valid_json():
    src = {"good": 1.5, "nan": float("nan"), "inf": float("inf"), "ninf": float("-inf")}
    out = _minify_for_prompt(src)
    assert out == {"good": 1.5}
    # No bare NaN / Infinity tokens — strict JSON round-trips cleanly.
    assert json.loads(json.dumps(out)) == {"good": 1.5}


def test_minify_list_keeps_positions_nan_becomes_null():
    out = _minify_for_prompt({"rows": [1.0, float("nan"), 3.0]})
    assert out["rows"] == [1.0, None, 3.0]


def test_minify_list_of_dicts_drops_none_keys_per_row():
    src = {"rows": [
        {"s": "2330", "pe": None, "v": 100},
        {"s": "2317", "pe": 12.3, "v": None},
    ]}
    out = _minify_for_prompt(src)
    assert out["rows"] == [{"s": "2330", "v": 100}, {"s": "2317", "pe": 12.3}]


def test_minify_is_idempotent():
    src = {"a": 1.230000001, "b": None, "c": [{"d": None, "e": 2.0}]}
    once = _minify_for_prompt(src)
    assert once == _minify_for_prompt(once)


def test_minify_shrinks_serialized_size():
    # End-to-end: a ctx-shaped dict with null noise serializes smaller.
    src = {
        "focus_briefs": [
            {"symbol": "2330", "pe": 18.5, "yield": None, "eps": None},
            {"symbol": "2317", "pe": None, "yield": 2.1, "eps": 5.0000000001},
        ],
        "taiwan_vix": {"value": 22.3, "from_value": None},
        "errors": [],
    }
    before = json.dumps(src, ensure_ascii=False, separators=(",", ":"))
    after = json.dumps(
        _minify_for_prompt(src), ensure_ascii=False, separators=(",", ":"),
    )
    assert len(after) < len(before)
    # errors:[] (meaningful "clean run" signal) is preserved.
    assert "errors" in _minify_for_prompt(src)


def test_measure_prompt_breakdown_sections_and_blocks():
    ctx = {"focus_briefs": [{"s": "2330"}], "macro": {"fed": 5.25}}
    context_json = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    base = "INSTRUCTIONS topicrules FRESH ANN " + context_json + " HIST"
    bd = measure_prompt_breakdown(
        system_prompt="SYS",
        base_user_prompt=base,
        topic="top",
        rules="rul",
        freshness="FRESH",
        annotation="ANN",
        context_json=context_json,
        history="HIST",
        tool_hint="",
        context_obj=ctx,
    )
    s = bd["sections"]
    assert s["system"] == 3
    assert s["context_json"] == len(context_json)
    assert s["history"] == 4
    assert s["topic_rules"] == 6
    assert s["instructions"] >= 0
    # total_chars is the sum of all sections (the invariant the UI relies
    # on to scale char counts to estimated tokens).
    assert bd["total_chars"] == sum(s.values())
    # Per-block sizes present + comparable (focus_briefs vs macro).
    assert set(bd["blocks"]) == {"focus_briefs", "macro"}
    assert bd["blocks"]["focus_briefs"] > 0


def test_measure_prompt_breakdown_instructions_never_negative():
    # Defensive: if substitutions somehow exceed the base length, the
    # remainder clamps at 0 rather than going negative.
    bd = measure_prompt_breakdown(
        system_prompt="",
        base_user_prompt="x",
        topic="aaaaa",
        rules="bbbbb",
        freshness="",
        annotation="",
        context_json="",
        history="",
        tool_hint="",
        context_obj={},
    )
    assert bd["sections"]["instructions"] == 0
    assert bd["blocks"] == {}
