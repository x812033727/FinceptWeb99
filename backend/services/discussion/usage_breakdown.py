"""Per-prompt-section + per-context-block size measurement for the
discussion "ctx usage detail" view.

`measure_prompt_breakdown` returns the character size of each logical
section of a persona's assembled prompt (system role / instructions /
market-context JSON / prior-turn history / ...) plus each top-level
context block (focus_briefs / news_sentiment / ...), so the UI can show
*where* the input went — `focus_briefs` is usually the single largest
block.

Character counts are objective and provider-agnostic. The provider only
ever reports one aggregate `prompt_tokens`, so the frontend scales these
char counts to *estimated* tokens using the persona's actual
`prompt_tokens` (clearly labelled "est"). The exact per-persona token /
cost numbers come from `llm_usage_events`, not from here.

Pure (no DB / LLM / I/O).
"""
from __future__ import annotations

import json
from typing import Any


def measure_prompt_breakdown(
    *,
    system_prompt: str,
    base_user_prompt: str,
    topic: str,
    rules: str,
    freshness: str,
    annotation: str,
    context_json: str,
    history: str,
    tool_hint: str,
    context_obj: dict[str, Any],
) -> dict[str, Any]:
    """Measure char sizes of each prompt section + each top-level context
    block.

    ``base_user_prompt`` is the rendered turn template WITHOUT the tool
    hint (which is appended separately). The ``instructions`` section —
    the static skeleton of the template (language rules, citation rules,
    formatting rules, task framing) — is derived as the remainder after
    subtracting the variable substitutions, so it needs no separate
    string to measure. ``context_json`` is the exact serialized string
    used in the prompt; ``context_obj`` is the same (already minified)
    dict, measured per top-level block for the breakdown.
    """
    variable = {
        "topic_rules": len(topic or "") + len(rules or ""),
        "freshness": len(freshness or ""),
        "annotation": len(annotation or ""),
        "context_json": len(context_json or ""),
        "history": len(history or ""),
    }
    instructions = max(0, len(base_user_prompt or "") - sum(variable.values()))
    sections = {
        "system": len(system_prompt or ""),
        **variable,
        "instructions": instructions,
        "tool_hint": len(tool_hint or ""),
    }
    blocks: dict[str, int] = {
        k: len(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
        for k, v in (context_obj or {}).items()
    }
    total_chars = sum(sections.values())
    return {"sections": sections, "blocks": blocks, "total_chars": total_chars}
