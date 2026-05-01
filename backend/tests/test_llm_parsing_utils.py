"""Tests for services.llm_parsing_utils — the shared LLM-output JSON
parser used by both the discussion synthesizer (PR #159) and the news
sentiment scorer (PR ④).

Cases here mirror the dialects observed in the wild from Gemini, Qwen,
DeepSeek-Reasoner, and OpenAI-compat providers."""
from __future__ import annotations

import json

import pytest

from services.llm_parsing_utils import (
    extract_json_object,
    loads_lenient,
    relax_json,
    strip_code_fence,
    strip_json_comments,
    strip_think_blocks,
)


# ── strip_think_blocks ────────────────────────────────────────────

def test_strip_think_blocks_removes_single_block():
    assert strip_think_blocks("hello <think>internal</think> world") == "hello  world"


def test_strip_think_blocks_handles_multiline():
    raw = "<think>\nstep 1\nstep 2\n</think>\n{\"x\": 1}"
    assert strip_think_blocks(raw) == "{\"x\": 1}"


def test_strip_think_blocks_is_case_insensitive():
    assert strip_think_blocks("<THINK>hidden</THINK>visible") == "visible"


def test_strip_think_blocks_handles_none():
    assert strip_think_blocks("") == ""


# ── strip_code_fence ──────────────────────────────────────────────

def test_strip_code_fence_unwraps_json_fence():
    raw = "```json\n{\"x\": 1}\n```"
    assert strip_code_fence(raw) == '{"x": 1}'


def test_strip_code_fence_unwraps_unlabelled_fence():
    raw = "```\n{\"x\": 1}\n```"
    assert strip_code_fence(raw) == '{"x": 1}'


def test_strip_code_fence_passthrough_when_no_fence():
    assert strip_code_fence('  {"x": 1}  ') == '{"x": 1}'


# ── extract_json_object ───────────────────────────────────────────

def test_extract_json_object_unwraps_surrounding_prose():
    raw = "Here is my analysis:\n{\"x\": 1}\nHope that helps."
    assert extract_json_object(raw) == '{"x": 1}'


def test_extract_json_object_respects_string_boundaries():
    raw = '{"text": "} not closing"} trailing'
    assert extract_json_object(raw) == '{"text": "} not closing"}'


def test_extract_json_object_returns_none_when_no_brace():
    assert extract_json_object("plain prose, no JSON") is None


# ── strip_json_comments + relax_json ──────────────────────────────

def test_strip_json_comments_removes_line_comments():
    raw = '{"x": 1, // hello\n"y": 2}'
    assert strip_json_comments(raw) == '{"x": 1, \n"y": 2}'


def test_strip_json_comments_removes_block_comments():
    raw = '{"x": /* note */ 1}'
    assert strip_json_comments(raw) == '{"x":  1}'


def test_strip_json_comments_preserves_double_slash_inside_strings():
    """Critical: URLs in string values must survive the stripper."""
    raw = '{"url": "https://example.com//path"}'
    assert strip_json_comments(raw) == raw


def test_relax_json_normalizes_full_width_punctuation():
    raw = "｛\"x\": 1，\"y\": 2｝"
    relaxed = relax_json(raw)
    assert "｛" not in relaxed and "｝" not in relaxed
    assert json.loads(relaxed) == {"x": 1, "y": 2}


def test_relax_json_drops_trailing_commas():
    raw = '{"a": [1, 2,], "b": {"c": 3,},}'
    assert json.loads(relax_json(raw)) == {"a": [1, 2], "b": {"c": 3}}


# ── loads_lenient (the integration entry point) ───────────────────

def test_loads_lenient_strict_path():
    assert loads_lenient('{"x": 1}') == {"x": 1}


def test_loads_lenient_recovers_from_line_comments():
    raw = '{"x": 1, // hint copied from prompt\n"y": 2}'
    assert loads_lenient(raw) == {"x": 1, "y": 2}


def test_loads_lenient_recovers_from_trailing_commas():
    assert loads_lenient('{"a": [1, 2,]}') == {"a": [1, 2]}


def test_loads_lenient_recovers_from_full_width_braces():
    raw = "｛\"x\": 1，\"y\": 2｝"
    assert loads_lenient(raw) == {"x": 1, "y": 2}


def test_loads_lenient_preserves_url_double_slash_in_value():
    raw = '{"url": "https://example.com"}'
    assert loads_lenient(raw) == {"url": "https://example.com"}


def test_loads_lenient_raises_on_truly_unparseable():
    with pytest.raises(json.JSONDecodeError):
        loads_lenient("not even close")
