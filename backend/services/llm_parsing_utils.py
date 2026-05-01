"""Tolerant JSON / text-cleanup helpers for LLM responses.

Three known classes of LLM artefact bite the structured-output flows
across this codebase (synthesizer, news sentiment scorer, persona
turn parser):

  - ``<think>…</think>`` chain-of-thought wrappers from reasoning
    models (DeepSeek-Reasoner, Qwen QwQ).
  - Markdown code fences (```json) around the actual payload.
  - "Relaxed JSON" dialects: ``//`` line comments, ``/* */`` block
    comments, trailing commas before ``}`` or ``]``, full-width
    braces / brackets / commas / colons (Chinese-leaning models).

This module is the single source of truth for handling them. Pulled
out of ``discussion_service`` so ``news_sentiment_service`` and any
future LLM-fed pipeline can reuse the exact same parser without
re-implementing.
"""
from __future__ import annotations

import json
import re
from typing import Any

_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def strip_think_blocks(text: str) -> str:
    """Remove every ``<think>…</think>`` block. Multi-line,
    case-insensitive, leaves text outside the tags untouched."""
    return _THINK_TAG_RE.sub("", text or "").strip()


def strip_code_fence(text: str) -> str:
    """Strip a single outer ```` ```json … ``` ```` (or unlabelled)
    code fence. Returns ``text`` unchanged if no fence."""
    text = text.strip()
    fence = _CODE_FENCE_RE.match(text)
    return fence.group(1).strip() if fence else text


def extract_json_object(text: str) -> str | None:
    """Return the first balanced top-level ``{…}`` substring of
    ``text`` (or ``None`` if no balanced object exists).

    Salvage path for models that wrap their JSON in surrounding prose
    ("Here is my analysis: {…} Hope this helps."). Tracks string
    boundaries so a ``}`` inside a JSON string doesn't break balance,
    and respects backslash escapes inside strings.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def strip_json_comments(text: str) -> str:
    """Remove ``//`` line and ``/* … */`` block comments while
    preserving the contents of JSON string literals — a naive regex
    pass would also chew up legitimate ``//`` chars in URL-shaped
    string values (``"https://example.com"``).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i + 2)
            i = n if j < 0 else j  # drop to EOL (keep the newline itself)
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def relax_json(text: str) -> str:
    """Defang the relaxed-JSON dialects LLMs love to emit:

      - ``//`` and ``/* */`` comments (often copied from a prompt
        template's range hints like ``// 最多5檔``)
      - Trailing commas before ``}`` / ``]``
      - Full-width Chinese braces / brackets / commas / colons
    """
    text = strip_json_comments(text)
    text = _TRAILING_COMMA_RE.sub(r"\1", text)
    return (
        text
        .replace("｛", "{").replace("｝", "}")
        .replace("［", "[").replace("］", "]")
        .replace("，", ",").replace("：", ":")
    )


def loads_lenient(text: str) -> Any:
    """Tolerant ``json.loads`` for LLM output.

    Layered fallbacks:
      1. ``json.loads(strict=False)`` — allows literal control chars
         inside strings (LLMs often emit real ``\\n`` newlines in
         Chinese content; strict JSON would reject).
      2. On ``JSONDecodeError``, apply :func:`relax_json` (comments,
         trailing commas, full-width punctuation) and retry.
    """
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        return json.loads(relax_json(text), strict=False)
