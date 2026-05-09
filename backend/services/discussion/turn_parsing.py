"""Pure parsers for the per-persona turn JSON wrapper.

A persona turn arrives as a JSON object the LLM has been told to
emit: ``{"stance": "agree|dissent|supplement", "content": "..."}``.
Three failure modes are routine:

  1. Reasoning models prepend a ``<think>...</think>`` block — the
     ``stream_chat`` filter drops it from SSE but the post-loop
     ``assembled`` string still carries it. Strip it here too.
  2. Some providers wrap the JSON in a ``\\`\\`\\`json`` code fence; lenient
     parsing handles that.
  3. The provider runs out of ``max_tokens`` mid-content, so the
     closing ``"}`` never appears. ``_extract_json_object`` returns
     None; ``_salvage_truncated_json`` rescues stance + partial
     content via two anchored regexes + a manual JSON-escape decoder.

All three helpers are pure (no DB, no LLM, no I/O), so unit tests
hit them directly. ``discussion_service`` re-exports the symbols
plus ``VALID_STANCES`` / ``DEFAULT_STANCE`` for back-compat with
``test_discussion_service.py``.
"""
from __future__ import annotations

import json
import re
from typing import Any

from services.llm_parsing_utils import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
    strip_code_fence as _strip_code_fence,
    strip_think_blocks,
)

VALID_STANCES = ("agree", "dissent", "supplement")
DEFAULT_STANCE = "supplement"


_CONTENT_OPEN_RE = re.compile(r'"content"\s*:\s*"')
_STANCE_RE = re.compile(r'"stance"\s*:\s*"([^"]*)"')

# Standard JSON escape sequences. Lookup is faster than a 6-way
# branch and the table reads naturally.
_JSON_ESCAPE_MAP = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def _decode_partial_json_string(s: str) -> str:
    """Decode JSON escape sequences inside a string fragment that has
    no closing quote (because the LLM hit max_tokens mid-content).
    Stops at the first unescaped `"` (legitimate end) or end of input.
    Drops a trailing partial escape (lone `\\` or incomplete `\\u####`)
    so the rendered text doesn't carry a dangling backslash."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                break
            esc = s[i + 1]
            mapped = _JSON_ESCAPE_MAP.get(esc)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
            if esc == "u":
                if i + 6 > n:
                    break
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                except ValueError:
                    out.append(s[i:i + 2])
                    i += 2
                continue
            out.append(esc)
            i += 2
        elif c == '"':
            break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _salvage_truncated_json(text: str) -> tuple[str, str] | None:
    """Recover (stance, content) from a JSON wrapper that got truncated
    when the LLM hit max_tokens mid-string.

    Triggered after `_extract_json_object` fails to find a balanced
    `{...}` — the closing `}` never appeared because the model ran out
    of budget while writing the content. The wrapper looks like
    `{"stance":"supplement","content":"...partial text`. We pull stance
    out by regex (it's a short word that almost always finishes before
    truncation hits) and take everything after `"content":"` as the
    content body, JSON-decoding the standard escapes so embedded `\\n`
    becomes a real newline.

    Returns None when neither a `"content":"` opener nor any sign of the
    JSON wrapper is present — the caller falls back to raw-text mode.
    """
    content_match = _CONTENT_OPEN_RE.search(text)
    if content_match is None:
        return None
    stance_match = _STANCE_RE.search(text[:content_match.start()])
    stance = stance_match.group(1) if stance_match else DEFAULT_STANCE
    content = _decode_partial_json_string(text[content_match.end():])
    return stance, content


def _parse_turn_response(raw: str) -> tuple[str, str]:
    """Return (stance, content). Falls back to (DEFAULT_STANCE, cleaned_raw)
    when the model drifts off JSON format — better to record the prose
    than to lose the turn entirely.

    Parsing is layered to survive the most common LLM shape drifts:
      1. strip `<think>...</think>` reasoning blocks
      2. strip surrounding markdown code fence
      3. parse with `strict=False` so embedded newlines / tabs in
         Chinese content don't blow up json
      4. if that fails, salvage the first balanced `{...}` object from
         surrounding prose (handles "Here is my analysis: {...}" cases)
      5. if no balanced object exists (LLM hit max_tokens mid-string so
         the closing `"}` never arrived), regex-extract the partial
         `content` field — keeps most of the persona's analysis instead
         of surfacing the raw `{"stance":"...","content":"...` wrapper
         to the user.
    """
    no_thinking = strip_think_blocks(raw)
    cleaned = _strip_code_fence(no_thinking)
    data: Any | None = None
    try:
        data = _loads_lenient(cleaned)
    except json.JSONDecodeError:
        salvaged = _extract_json_object(cleaned)
        if salvaged is not None:
            try:
                data = _loads_lenient(salvaged)
            except json.JSONDecodeError:
                data = None
    if isinstance(data, dict):
        stance = str(data.get("stance", "")).strip().lower()
        if stance not in VALID_STANCES:
            stance = DEFAULT_STANCE
        content = str(data.get("content", "")).strip()
        return stance, content

    truncated = _salvage_truncated_json(cleaned)
    if truncated is not None:
        stance, content = truncated
        stance = stance.strip().lower()
        if stance not in VALID_STANCES:
            stance = DEFAULT_STANCE
        content = content.strip()
        if content:
            return stance, content
    return DEFAULT_STANCE, no_thinking.strip()
