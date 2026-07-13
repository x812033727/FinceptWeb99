"""Per-turn execution primitives for the round runner.

``_ask_persona`` (the per-persona LLM streaming wrapper) plus the two
stateful helpers it and the round loop own — ``_ThinkBlockFilter``
(drops reasoning-model ``<think>...</think>`` blocks from the SSE
feed) and ``TurnEvent`` (the dataclass wrapper for each yielded
event) — and ``_PERSONA_TOOL_USAGE_HINT`` (the tool-availability
prompt section). Pure move out of the former single-module
``round_runner.py`` (R6 PR1); see the package docstring for the
import-layering rules.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import DiscussionTurn
from services.discussion.ctx_minify import _minify_for_prompt
from services.discussion.persona_config import (
    _build_persona_tool_kwargs,
    _filter_context_for_persona,
)
from services.discussion.prompts import (
    _TURN_PROMPT_TEMPLATE,
    _format_freshness_preamble,
    _persona_schema_annotation,
)
from services.discussion.transcript_format import _format_history
from services.discussion.usage_breakdown import measure_prompt_breakdown

if TYPE_CHECKING:
    from ai.agents import AgentSpec

log = logging.getLogger(__name__)


# ── <think> block streaming filter ──────────────────────────────────
#
# Reasoning models surface their chain-of-thought wrapped in
# ``<think>...</think>`` blocks. We drop them as a streaming SSE filter
# so the thinking never flashes across the chat UI. The post-hoc
# parser ``strip_think_blocks`` (in ``services.llm_parsing_utils``) is
# a second-pass safety net used during turn-content persistence.


class _ThinkBlockFilter:
    """Stateful streaming filter that drops text inside `<think>...</think>`
    tags as deltas arrive. Keeps the raw model chunks out of the user-
    visible SSE stream, so reasoning models don't flash hundreds of lines
    of internal monologue across the chat UI before settling on the final
    JSON.

    Buffers up to 16 chars of trailing text in case the next chunk
    completes a `<think>` opening tag straddling the boundary.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _MAX_OPEN_LEN = len(_OPEN)
    _MAX_CLOSE_LEN = len(_CLOSE)

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, chunk: str) -> str:
        """Consume `chunk`; return the filtered text safe to forward."""
        self._buf += chunk
        out_parts: list[str] = []
        while self._buf:
            if self._in_think:
                end = self._buf.find(self._CLOSE)
                if end < 0:
                    # Still inside thinking; hold last few chars in case
                    # `</think>` straddles the next chunk boundary.
                    keep = min(len(self._buf), self._MAX_CLOSE_LEN - 1)
                    self._buf = self._buf[-keep:] if keep else ""
                    break
                self._in_think = False
                self._buf = self._buf[end + self._MAX_CLOSE_LEN:]
            else:
                start = self._buf.find(self._OPEN)
                if start < 0:
                    # No `<think>` opener found. Emit everything except a
                    # tail that is a *prefix* of `<think>` (e.g. `<thi`)
                    # which might complete on the next chunk. A bare `<`
                    # followed by unrelated text is fine to release.
                    hold = 0
                    max_check = min(len(self._buf), self._MAX_OPEN_LEN - 1)
                    for i in range(max_check, 0, -1):
                        if self._OPEN.startswith(self._buf[-i:]):
                            hold = i
                            break
                    if hold:
                        out_parts.append(self._buf[:-hold])
                        self._buf = self._buf[-hold:]
                    else:
                        out_parts.append(self._buf)
                        self._buf = ""
                    break
                out_parts.append(self._buf[:start])
                self._in_think = True
                self._buf = self._buf[start + self._MAX_OPEN_LEN:]
        return "".join(out_parts)

    def flush(self) -> str:
        """Emit any held-over text once the stream is done. If we're still
        inside a `<think>` block (model never closed it), drop the tail."""
        if self._in_think:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out


# ── SSE event wrapper ───────────────────────────────────────────────


@dataclass(frozen=True)
class TurnEvent:
    """Single event emitted by `run_round` for SSE serialization."""
    type: str
    payload: dict[str, Any]


# ── per-persona tool-usage hint ────────────────────────────────────
#
# Appended to the user prompt when the persona has tools available so
# the LLM is reminded that fabricating numbers is never necessary.
# Listed tool names mirror what `build_toolset` / `build_openai_compat_
# toolset` ship.

_PERSONA_TOOL_USAGE_HINT = (
    "\n\n## 工具可用\n"
    "你本回合可以呼叫下列工具取得即時 / 歷史數據：\n"
    "- `get_quote`（單檔即時報價）\n"
    "- `compare_quotes`（多檔並排報價，最多 10 檔；省 max_turns 預算）\n"
    "- `get_options_chain`（美股選擇權鏈，含 strike / IV / OI；TW 不支援）\n"
    "- `get_symbol_news`（指定標的最近新聞，可指定 limit ≤ 20）\n"
    "- `get_symbol_sentiment`（指定標的歷史情緒分數聚合，"
    "可調 max_age_hours ≤ 720）\n"
    "- `get_peers`（同業比較，TW 限定，回傳 5-10 檔同產業標的的 PE / PB / "
    "現金股息殖利率）\n"
    "- `get_financials`（三大財報；US 取最近 5 期年度，TW 取近 60 列 "
    "FinMind 季度資料，可推 ROE / 毛利率 / 槓桿 / FCF）\n"
    "- `get_institutional_history`（TW 限定，法人買賣超日序列，days ≤ 90）\n"
    "- `get_margin_history`（TW 限定，融資融券日序列，days ≤ 90）\n"
    "- `get_top_brokers`（TW 限定，主力分點 top buyers + top sellers，"
    "用於辨識特定券商分點的累積 / 派發）\n"
    "- `get_taifex_positioning`（TW 限定，期指三大法人未平倉快照 + 5 日變化，"
    "default contract=TX；外資未平倉領先大盤約 1-2 日）\n"
    "- `run_dcf` / `run_var` / `run_backtest`（分析運算）\n"
    "- `query_user_data`（使用者自身資料，限本人）\n"
    "**禁止虛構數據** — 若需要某個數字而 `## 市場現況` 與 `focus_briefs` 找不到，"
    "請呼叫對應工具，再把結果寫進 content。每次工具呼叫會自動計入流程，"
    "你只需專注在分析。"
)


# ── per-persona LLM streaming ──────────────────────────────────────


async def _ask_persona(
    db: AsyncSession,
    *,
    spec: AgentSpec,
    persona_id: str,
    topic: str,
    rules: str,
    context: dict[str, Any],
    prior_turns: list[DiscussionTurn],
    user_id: str | None,
    user_role: str | None = None,
    as_of_date: date | None = None,
    interject_question: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Yield raw stream events from one persona's turn. Caller assembles
    the deltas + parses the final JSON.

    `interject_question` (B4): when the owner interjected a question and
    the moderator assigned THIS persona to answer it, the question is
    appended as a dedicated prompt section so the persona answers it
    directly instead of producing another generic stance statement. The
    question itself is also already in `prior_turns` (persisted as a
    `user_input` turn before this call), so later personas see it in
    their history either way.

    Takes a pre-resolved `AgentSpec` so callers can batch-load the
    persona roster's overrides up-front (avoiding an N+1 round trip
    inside the per-persona loop). When the persona's resolved provider
    supports tools and the discussion's owner has the right role,
    `_build_persona_tool_kwargs` adds the MCP / OpenAI-compat toolset
    so the persona can call get_quote / run_dcf / run_var / run_backtest
    / query_user_data instead of guessing from the static context.

    `persona_id` is the canonical agent ID (e.g. `buffett`,
    `macro_analyst`) — used to look up the per-persona context profile
    so the LLM only sees the blocks its archetype actually uses.

    `as_of_date` (backtest mode): forwarded to the toolset builder so
    TW chip-flow tools anchor at the historical date instead of today.
    """
    # `stream_chat` is lazy-imported via `services.discussion_service`'s
    # namespace (rather than directly from `ai.llm_router`) so the
    # ~38 test sites that `patch("services.discussion_service.stream_chat",
    # ...)` to mock the LLM continue to land on the binding the code
    # actually reads. Same pattern α (synthesizer) used.
    from services.discussion_service import stream_chat

    tool_kwargs = _build_persona_tool_kwargs(
        provider=spec.default_provider,
        user_role=user_role,
        user_id=user_id,
        as_of_date=as_of_date,
    )
    # Filter context down to blocks this persona actually uses — saves
    # tokens and stops weak models from mixing irrelevant blocks (e.g.
    # macro_analyst citing risk_warnings dispositions in a Fed thesis).
    # Filter to the persona's blocks, then losslessly minify (drop null
    # leaves + collapse float noise) before serialization. The ctx JSON
    # is re-sent on every tool-loop iteration, so this trims every
    # iteration with zero semantic change. Annotation is built from the
    # minified ctx so it only advertises blocks that survived.
    filtered_ctx = _minify_for_prompt(
        _filter_context_for_persona(context, persona_id)
    )
    annotation = _persona_schema_annotation(filtered_ctx)
    freshness = _format_freshness_preamble(context)
    # Compact separators (no indent/whitespace) — see the tool-loop
    # re-send note above.
    context_json = json.dumps(
        filtered_ctx, ensure_ascii=False, separators=(",", ":"),
    )
    history = _format_history(prior_turns)
    base_user_prompt = _TURN_PROMPT_TEMPLATE.format(
        topic=topic,
        rules=rules,
        annotation=annotation,
        freshness_preamble=freshness,
        context=context_json,
        history=history,
    )
    tool_hint = _PERSONA_TOOL_USAGE_HINT if tool_kwargs else ""
    interject_block = (
        (
            "\n\n## 使用者插話\n"
            "討論發起人剛剛插話提問，主持人指定由你回答。"
            "請以你的角色直接、具體地回答這個問題（可引用 ## 市場現況 "
            "或工具數據），不需要重複完整的立場論述，"
            "輸出格式仍維持原本要求的 JSON：\n"
            f"{interject_question}"
        )
        if interject_question
        else ""
    )
    user_prompt = base_user_prompt + tool_hint + interject_block
    # Per-section + per-block input-size breakdown for the "ctx 用量明細"
    # view. Yielded as a synthetic event the round runner captures (folds
    # into turn_end + persists on the turn). Char-based + provider-
    # agnostic; the UI scales it to est tokens using the persona's real
    # prompt_tokens.
    yield {
        "type": "input_breakdown",
        "breakdown": measure_prompt_breakdown(
            system_prompt=spec.system_prompt,
            base_user_prompt=base_user_prompt,
            topic=topic,
            rules=rules,
            freshness=freshness,
            annotation=annotation,
            context_json=context_json,
            history=history,
            tool_hint=tool_hint,
            context_obj=filtered_ctx,
        ),
    }
    messages = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        from services.runtime_config_service import get_int as _runtime_get_int
        turn_max_tokens = await _runtime_get_int(db, "DISCUSSION_TURN_MAX_TOKENS")
    except Exception as exc:
        log.warning("discussion.runtime_config_failed",
                    extra={"setting": "DISCUSSION_TURN_MAX_TOKENS", "error": str(exc)})
        turn_max_tokens = settings.DISCUSSION_TURN_MAX_TOKENS
    async for event in stream_chat(
        messages=messages,
        provider=spec.default_provider,
        model=spec.default_model,
        max_tokens=turn_max_tokens,
        temperature=0.4,
        db=db,
        user_id=user_id,
        **tool_kwargs,
    ):
        yield event
