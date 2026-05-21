"""Win-band lesson extraction for backtest discussions.

PR-2 closed the production-win gap: the post-mortem flow used to
``return`` early on wins to save N personas × LLM cost per winning
date, leaving the learning loop one-sided (only miss-mode lessons
accumulated). This module replaces the missing path with a single
synthesizer-style LLM call given the win-band prompt, extracting a
structured ``lessons`` array and persisting it via
``discussion_lesson_service.extract_and_persist_lessons``.

Lifted out of the monolithic ``discussion_service`` so the entrypoint
+ its tests have a focused home; the symbol is re-exported from
``discussion_service`` for back-compat with existing call sites
(``backtest_sweep_service`` + the seven ``test_discussion_*`` files).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ai.llm_router import stream_chat
from models.discussion import Discussion
from services.llm_parsing_utils import (
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
    strip_code_fence as _strip_code_fence,
    strip_think_blocks,
)

log = logging.getLogger(__name__)


def _extract_lessons_payload(raw: str) -> Any:
    """Re-parse the synthesizer's raw output to surface the optional
    `lessons` array. `_safe_conclusion` strips it out (only the
    five canonical fields are kept) so we re-run the lenient parse
    here. Returns the raw `lessons` value (`list` when the model
    obeyed; anything else is filtered downstream)."""
    try:
        cleaned = _strip_code_fence(strip_think_blocks(raw))
        data = _loads_lenient(cleaned)
    except (json.JSONDecodeError, ValueError):
        salvaged = _extract_json_object(cleaned) if cleaned else None
        if salvaged is None:
            return None
        try:
            data = _loads_lenient(salvaged)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    return data.get("lessons")


async def extract_winning_thesis_lessons(
    db: AsyncSession,
    discussion: Discussion,
    *,
    win_prompt_text: str,
    user_id: str | None = None,
) -> int:
    """PR-2: extract structured `lessons` from a backtest discussion
    that hit its win threshold WITHOUT running a full critique
    round. Single LLM call given the win-band prompt
    (`format_post_mortem_win_prompt`'s output, which already carries
    the D1-D5 numbers + verdict + missed-winners table).

    Closes the production-win gap: the post-mortem flow used to
    `return` early on wins to save N personas × LLM cost per winning
    date, leaving the learning loop one-sided (only miss-mode
    lessons accumulated). One additional LLM call per win is the
    minimum cost to record what worked.

    Returns the number of lesson rows written (0..5). Best-effort
    on every error path: LLM failure / parse failure / persist
    failure all log + return 0; the sweep keeps going.

    Skipped silently when:
      - `discussion.as_of_date` is None (live discussion — lessons
        are only meaningful for backtest replay)
      - `discussion.conclusion` is missing or marked `_parse_error`
      - the win prompt text is empty (no D1-D5 data resolved yet)
    """
    if discussion.as_of_date is None:
        return 0
    if not discussion.conclusion or discussion.conclusion.get("_parse_error"):
        return 0
    if not win_prompt_text:
        return 0

    from services.system_task_config_service import resolve as _resolve_task
    provider, model = await _resolve_task(db, "discussion_synthesizer")

    # The win-band prompt asks the personas to reflect; here we
    # collapse N reflection round + re-synth into a single LLM call
    # whose only job is to emit the lessons array. The system prompt
    # is intentionally narrow so the model doesn't try to also
    # rewrite the conclusion or invent confidence numbers.
    sys_prompt = (
        "你是回測命中經驗的歸檔員。閱讀「事後檢討 — 命中經驗」反思題目，"
        "把這次命中的可重用教訓整理成結構化 JSON。"
        "**所有輸出必須使用繁體中文（台灣用語）**。"
    )
    user_prompt = win_prompt_text + (
        "\n\n## 任務\n"
        "**直接輸出合法 JSON**（不要 markdown fence、不要解釋、不要註解）：\n"
        '{ "lessons": [\n'
        '  {"category": "correct_signal_combo", "lesson_text": "...≤80字...",\n'
        '   "related_symbols": ["..."], "missed_winners": ["..."]}\n'
        "] }\n\n"
        "欄位規則：\n"
        "- 最多 5 條，重要先放\n"
        "- category 七選一：correct_signal_combo | successful_thesis | "
        "regime_context | regime_capture | regime_signal | "
        "missing_signal_category | other\n"
        "  其中 regime_context / regime_capture / regime_signal 用於\n"
        "  大勝但其實是 regime-riding 的場合，協助下次討論辨識同類 regime\n"
        "- lesson_text ≤ 80 字、繁體中文、要點名具體訊號 / 產業 / 股票，"
        "避免「下次要更小心」這類空話\n"
        "- 沒有真正可重用的教訓（純 luck）→ 回 `\"lessons\": []`，不要硬擠"
    )

    assembled = ""
    try:
        async for event in stream_chat(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            provider=provider,
            model=model,
            max_tokens=2048,
            temperature=0.2,
            db=db,
            user_id=user_id,
        ):
            etype = event.get("type")
            if etype == "delta":
                assembled += event.get("text", "")
            elif etype == "error":
                log.warning(
                    "discussion.win_lesson.llm_error",
                    extra={
                        "discussion_id": str(discussion.id),
                        "message": event.get("message"),
                    },
                )
                break
    except Exception:
        log.exception(
            "discussion.win_lesson.stream_failed",
            extra={"discussion_id": str(discussion.id)},
        )
        return 0

    raw_obj = _extract_lessons_payload(assembled)
    if not raw_obj:
        return 0

    try:
        from services.discussion_lesson_service import (
            extract_and_persist_lessons,
        )
        written = await extract_and_persist_lessons(
            db,
            discussion_id=discussion.id,
            owner_user_id=discussion.owner_id,
            market=discussion.market,
            as_of_date=discussion.as_of_date,
            lessons_payload=raw_obj,
            ctx=None,
            verdict="win",
        )
        return len(written)
    except Exception:
        log.exception(
            "discussion.win_lesson.persist_failed",
            extra={"discussion_id": str(discussion.id)},
        )
        return 0
