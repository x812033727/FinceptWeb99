"""Pure parsers for the synthesizer's conclusion JSON.

Four functions extracted from ``discussion_service`` so the
parse-and-canonicalize pipeline lives in one focused module —
``synthesize_conclusion`` orchestrates LLM I/O, this file owns the
"text → typed dict" transformation:

  - ``_try_repair_truncated_json`` — best-effort recovery for output
    truncated mid-emission (LLM hits ``max_tokens`` while writing
    the object). Walks bracket / string state and closes any
    dangling brackets so the result is parseable.
  - ``_safe_conclusion`` — top-level parser. Strips ``<think>``
    blocks + markdown fences, attempts ``_loads_lenient``, then
    ``_extract_json_object``, then the truncation repair, finally
    falls back to a parse-error placeholder. Canonicalises the five
    public fields (recommendations / reasoning / risks /
    time_horizon / consensus_score) so downstream consumers get
    a stable shape.
  - ``_parse_recommendations`` — validates the
    ``recommendations: [{symbol, confidence}]`` array. Bad
    confidences clamp to 0.5; symbols dedup; cap at 5.
  - ``compute_conclusion_diff`` — structured delta between an
    original conclusion and a post-mortem revised one. Used by
    PR-2's post-mortem diff column so the UI can render
    "what changed" without recomputing.

All four are pure functions (no DB / LLM / I/O), so the module
imports are lightweight and the unit tests can hit them directly.
``discussion_service`` re-exports the symbols for back-compat with
its existing call sites + the ``test_discussion_conclusion_diff``
test file.
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


def _try_repair_truncated_json(text: str) -> str | None:
    """Best-effort recovery for JSON output that got truncated mid-
    emission (typical when an LLM hits `max_tokens` while writing the
    object). Walks from the first `{`, tracks string + bracket depth,
    and on EOF closes any open string + dangling brackets so the
    result is parseable.

    Returns the repaired substring, or None when the input has no
    `{` to anchor on. Caller still pipes through `_loads_lenient` —
    repair only fixes the structural truncation, not relaxed-JSON
    quirks (those layers stack).

    Conservative: trims trailing `,` / `:` since closing immediately
    after either is invalid. The recovered fields may be partial
    (e.g. `reasoning` cut mid-sentence), but a partial conclusion
    is infinitely more useful than a parse-error placeholder for
    the operator triaging the discussion.
    """
    start = text.find("{")
    if start < 0:
        return None
    in_string = False
    escape = False
    bracket_stack: list[str] = []   # "{" / "["
    last_real_char = ""
    for c in text[start:]:
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
            last_real_char = c
            continue
        if c.isspace():
            continue
        last_real_char = c
        if c in "{[":
            bracket_stack.append(c)
        elif c == "}":
            if bracket_stack and bracket_stack[-1] == "{":
                bracket_stack.pop()
        elif c == "]":
            if bracket_stack and bracket_stack[-1] == "[":
                bracket_stack.pop()

    if not in_string and not bracket_stack:
        return None   # already balanced — caller's earlier pass covers it

    body = text[start:].rstrip()
    # Drop a dangling trailing comma or colon (illegal right before
    # a close-bracket).
    while body and body[-1] in ",:":
        body = body[:-1].rstrip()

    suffix = ""
    if in_string:
        suffix += '"'
    # If we ended right after a `:` (key with no value), drop the
    # whole key/value pair to avoid `"foo":}` which is illegal.
    if last_real_char == ":":
        # Walk back to the last `,` or `{` and trim the orphan key.
        for i in range(len(body) - 1, -1, -1):
            if body[i] in ",{":
                body = body[:i + 1] if body[i] == "," else body[:i + 1]
                if body and body[-1] == ",":
                    body = body[:-1]
                break
    for opener in reversed(bracket_stack):
        suffix += "}" if opener == "{" else "]"
    return body + suffix


def _safe_conclusion(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(strip_think_blocks(raw))
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
    # PR #267: last-resort repair for truncated JSON. Reasoning
    # models (M2.7 etc.) under post-mortem load can dump a long
    # chain-of-thought + start the JSON, then run out of
    # `max_tokens` mid-object — `_extract_json_object` returns
    # None because the closing `}` was never emitted. Try closing
    # any open string + brackets and re-parse before falling
    # through to the parse-error placeholder.
    if data is None:
        repaired = _try_repair_truncated_json(cleaned)
        if repaired is not None:
            try:
                data = _loads_lenient(repaired)
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return {
            "recommended_symbols": [],
            "recommendations": [],
            "reasoning": raw.strip()[:500] or "無法解析結論",
            "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.0,
            # A parse failure is never a deliberate abstention — the
            # verifier must keep grading these as `unverifiable`.
            "abstained": False,
            "abstain_reason": "",
            "_parse_error": True,
        }

    # PR-C0: prefer the structured `recommendations: [{symbol, confidence}]`
    # shape (each pick gets a per-symbol confidence). Old discussions and
    # LLMs that miss the field fall through to the flat
    # `recommended_symbols` list with a neutral 0.5 confidence so
    # downstream Brier scoring (PR-C1) can always assume the structured
    # shape exists.
    recommendations = _parse_recommendations(data)
    if not recommendations:
        legacy_symbols = data.get("recommended_symbols") or []
        if isinstance(legacy_symbols, list):
            seen: set[str] = set()
            for raw_symbol in legacy_symbols:
                symbol = str(raw_symbol).strip()
                if not symbol or symbol in seen:
                    continue
                seen.add(symbol)
                recommendations.append({"symbol": symbol, "confidence": 0.5})
                if len(recommendations) >= 5:
                    break
    recommended_symbols = [entry["symbol"] for entry in recommendations]

    risks = data.get("risks") or []
    if not isinstance(risks, list):
        risks = []
    horizon = str(data.get("time_horizon", "short_term"))
    if horizon not in ("short_term", "medium_term", "long_term"):
        horizon = "short_term"
    try:
        consensus = float(data.get("consensus_score", 0.0))
    except (TypeError, ValueError):
        consensus = 0.0
    consensus = max(0.0, min(1.0, consensus))
    # An empty recommendation list has two very different meanings and
    # the verifier grades them differently: a deliberate "no setup
    # today" (`abstain`) is a legitimate outcome, while a synthesizer
    # that simply failed to emit symbols is `unverifiable`. Only the
    # LLM's explicit structured flag distinguishes them — never infer
    # it from `reasoning` prose, which misfires both ways (a
    # discussion can discuss abstaining and still recommend).
    #
    # Recommendations win the tie: a row with picks is not an
    # abstention no matter what the flag says.
    abstained = bool(data.get("abstained")) and not recommendations
    abstain_reason = str(data.get("abstain_reason", ""))[:500] if abstained else ""
    return {
        "recommended_symbols": recommended_symbols,
        "recommendations": recommendations,
        "reasoning": str(data.get("reasoning", ""))[:1000],
        "risks": [str(r).strip() for r in risks if str(r).strip()][:10],
        "time_horizon": horizon,
        "consensus_score": round(consensus, 3),
        "abstained": abstained,
        "abstain_reason": abstain_reason,
    }


def _parse_recommendations(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse and validate the `recommendations` list out of the
    synthesizer JSON. Each entry must have a non-empty `symbol`; bad
    `confidence` values (string, NaN, out of range) clamp to [0, 1] with
    a neutral 0.5 fallback. Symbols dedup by first occurrence and the
    list is capped at 5 to match the historical `recommended_symbols`
    cap. Returns `[]` when the field is missing or unusable so the
    caller can fall back to the legacy flat list.
    """
    raw = data.get("recommendations")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol", "")).strip()
        if not symbol or symbol in seen:
            continue
        try:
            conf = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        if conf != conf:   # NaN guard
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        seen.add(symbol)
        out.append({"symbol": symbol, "confidence": round(conf, 3)})
        if len(out) >= 5:
            break
    return out


def compute_conclusion_diff(
    orig: dict[str, Any] | None,
    post: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """PR-2: structured delta between an original conclusion and a
    post-mortem revised one. Returns None when either side is missing
    or unparseable so the caller can decide whether to skip writing
    the column.

    Shape returned (only non-trivial fields included):
      - symbols_added: symbols in post but not orig
      - symbols_removed: symbols in orig but not post
      - confidence_changes: per-symbol {orig, post, delta} for
        symbols present on BOTH sides where confidence shifted
        (>= 0.05 absolute, suppress noise)
      - consensus_score_delta: post - orig
      - time_horizon_changed: bool
      - reasoning_overlap: Jaccard index over whitespace-tokenised
        reasoning text (rough but cheap proxy for "how much did the
        narrative change")

    Defensive on bad input: parse_error placeholders, empty dicts,
    None — all return None so the column stays NULL rather than
    writing a misleading half-diff.
    """
    if not isinstance(orig, dict) or not isinstance(post, dict):
        return None
    if orig.get("_parse_error") or post.get("_parse_error"):
        return None

    def _recs(c: dict[str, Any]) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in c.get("recommendations") or []:
            if not isinstance(r, dict):
                continue
            symbol = str(r.get("symbol", "")).strip()
            if not symbol:
                continue
            try:
                out[symbol] = float(r.get("confidence", 0.5))
            except (TypeError, ValueError):
                out[symbol] = 0.5
        return out

    orig_recs = _recs(orig)
    post_recs = _recs(post)
    orig_set, post_set = set(orig_recs), set(post_recs)
    symbols_added = sorted(post_set - orig_set)
    symbols_removed = sorted(orig_set - post_set)
    confidence_changes: dict[str, dict[str, float]] = {}
    for sym in sorted(orig_set & post_set):
        o, p = orig_recs[sym], post_recs[sym]
        if abs(p - o) >= 0.05:
            confidence_changes[sym] = {
                "orig": round(o, 3),
                "post": round(p, 3),
                "delta": round(p - o, 3),
            }

    try:
        orig_consensus = float(orig.get("consensus_score", 0.0))
    except (TypeError, ValueError):
        orig_consensus = 0.0
    try:
        post_consensus = float(post.get("consensus_score", 0.0))
    except (TypeError, ValueError):
        post_consensus = 0.0

    horizon_changed = (
        str(orig.get("time_horizon", "")).strip()
        != str(post.get("time_horizon", "")).strip()
    )

    def _tokens(text: str) -> set[str]:
        # Cheap jaccard proxy. Hyphen-and-digit-aware split so
        # symbol codes (2330) and percentages (-5%) survive as
        # single tokens; lowercase for English; min token length
        # 2 to drop noise like single Chinese punctuation.
        return {
            tok for tok in re.split(r"[\s,。、；;:.!?「」（）()/]+", text.lower())
            if len(tok) >= 2
        }

    orig_tokens = _tokens(str(orig.get("reasoning", "")))
    post_tokens = _tokens(str(post.get("reasoning", "")))
    if orig_tokens or post_tokens:
        union = len(orig_tokens | post_tokens)
        overlap = (
            round(len(orig_tokens & post_tokens) / union, 3) if union else 1.0
        )
    else:
        overlap = 1.0

    return {
        "symbols_added": symbols_added,
        "symbols_removed": symbols_removed,
        "confidence_changes": confidence_changes,
        "consensus_score_delta": round(post_consensus - orig_consensus, 3),
        "time_horizon_changed": horizon_changed,
        "reasoning_overlap": overlap,
    }
