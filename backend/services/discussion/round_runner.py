"""SSE round runner: ``run_round`` (the per-discussion async generator
that drives one persona-by-persona turn loop) plus ``_ask_persona``
(the per-persona LLM streaming wrapper) and the two stateful
helpers they own — ``_ThinkBlockFilter`` (drops reasoning-model
``<think>...</think>`` blocks from the SSE feed) and ``TurnEvent``
(the dataclass wrapper for each yielded event).

Extracted from ``services.discussion_service`` as the C3-1 γ slice in
``misty-mixing-harbor.md``. After β moved out the context-assembly
blocks and α moved the synthesiser, this γ slice covers the single
largest remaining function in the file (~480 LOC for ``run_round``
alone) and brings ``discussion_service.py`` from ~1480 LOC under
~900 LOC.

Imports are layered the same way the other ``services/discussion/``
modules do it:

  * Top-level: the already-extracted helper modules
    (``discussion.persona_config``, ``discussion.prompts``,
    ``discussion.transcript_format``, ``discussion.turn_parsing``,
    ``discussion.symbols``).
  * Lazy (function-local) from ``services.discussion_service``:
    ``STATUS_DRAFT`` / ``STATUS_RUNNING``, ``gather_market_context``,
    ``get_turns``, ``_upsert_round_context``, ``stream_chat``. These
    stay in ``discussion_service`` proper so the ~38 test sites that
    do ``patch("services.discussion_service.stream_chat", ...)`` (and
    similar) continue to land on the binding the running code reads.
    Same pattern α (synthesiser) and β (context_assembly) used.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import Discussion, DiscussionTurn
from services.discussion.persona_config import (
    _build_persona_tool_kwargs,
    _filter_context_for_persona,
    _resolve_persona_specs,
)
from services.discussion.prompts import (
    _TURN_PROMPT_TEMPLATE,
    _format_freshness_preamble,
    _persona_schema_annotation,
)
from services.discussion.symbols import extract_focus_symbols
from services.discussion.transcript_format import _format_history
from services.discussion.turn_parsing import _parse_turn_response

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
) -> AsyncGenerator[dict, None]:
    """Yield raw stream events from one persona's turn. Caller assembles
    the deltas + parses the final JSON.

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
    filtered_ctx = _filter_context_for_persona(context, persona_id)
    # Build the schema annotation from what the persona actually sees,
    # not the full block list — otherwise the prompt advertises blocks
    # the LLM can't find in `## 市場現況` and forces it to either
    # hallucinate values or apologise about missing data.
    annotation = _persona_schema_annotation(filtered_ctx)
    user_prompt = _TURN_PROMPT_TEMPLATE.format(
        topic=topic,
        rules=rules,
        annotation=annotation,
        freshness_preamble=_format_freshness_preamble(context),
        context=json.dumps(filtered_ctx, ensure_ascii=False, indent=2),
        history=_format_history(prior_turns),
    )
    if tool_kwargs:
        user_prompt += _PERSONA_TOOL_USAGE_HINT
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


# ── per-round SSE orchestrator ─────────────────────────────────────


async def run_round(
    db: AsyncSession,
    discussion: Discussion,
    *,
    user_id: str | None = None,
    user_role: str | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> AsyncGenerator[TurnEvent, None]:
    """Run one full round of discussion. Each persona is queried in order;
    each persona's response is persisted as a `DiscussionTurn` row.

    Emits the following event types:
      - round_start  {round}
      - context      {market_context}
      - turn_start   {round, turn_index, persona_id, persona_name}
      - delta        {round, turn_index, persona_id, text}
      - tool_call    {round, turn_index, persona_id, id, name, args}
      - tool_result  {round, turn_index, persona_id, id, name, summary, is_error}
      - turn_end     {round, turn_index, persona_id, stance, content}
      - round_end    {round, turn_count}
      - error        {message}            (terminal)

    `user_role` (the discussion owner's role) gates tool availability
    on tool-capable providers — analyst / admin get a real toolset,
    viewers fall through to plain streaming. Providers without tool
    support ignore the role entirely.
    """
    # CRUD entry-points + status sentinels stay in `discussion_service`
    # proper; lazy-import keeps the load graph acyclic and (for
    # `gather_market_context` / `get_turns` / `_upsert_round_context`)
    # preserves the ~38 test sites that
    # `patch("services.discussion_service.X", ...)`.
    from services.discussion_service import (
        STATUS_DRAFT,
        STATUS_RUNNING,
        _upsert_round_context,
        gather_market_context,
        get_turns,
    )

    # Atomic SQL increment so the round counter can't drift from what's
    # in the DB. The previous in-memory `discussion.current_round =
    # round_number` + commit relied on SQLAlchemy attribute-tracking +
    # session attachment + autoflush behaviour, all of which can fail
    # silently (e.g. detached entity from a stale request, autoflush=False
    # interaction). Doing it as `UPDATE ... SET current_round =
    # current_round + 1 RETURNING current_round` is bulletproof: one
    # round-trip, atomic, returns the new value directly. We mirror the
    # new values onto the in-memory entity manually — `db.refresh()`
    # would raise "Instance is not persistent" after an ORM update that
    # SQLAlchemy 2.0 chose to synchronize via expunge under PostgreSQL.
    now = datetime.now(UTC)
    result = await db.execute(
        update(Discussion)
        .where(Discussion.id == discussion.id)
        .values(
            current_round=Discussion.current_round + 1,
            status=STATUS_RUNNING,
            updated_at=now,
        )
        .returning(Discussion.current_round)
        .execution_options(synchronize_session=False)
    )
    round_number = result.scalar_one()
    await db.commit()
    discussion.current_round = round_number
    discussion.status = STATUS_RUNNING
    discussion.updated_at = now
    log.info(
        "discussion.round.started",
        extra={"discussion_id": str(discussion.id), "round": round_number},
    )

    yield TurnEvent("round_start", {"round": round_number})

    # Try-finally guarantees status returns to DRAFT even if an
    # unexpected exception fires below (e.g. a transient DB commit failure
    # while persisting a turn). Without this the discussion would be
    # permanently stuck in RUNNING and the router would reject every
    # subsequent /round call.
    try:
        focus = extract_focus_symbols(
            discussion.topic, market=discussion.market,
        )
        # Bridge ctx-gathering progress milestones to SSE events so
        # the frontend's preparing card (PR #244) can show
        # "scoring news sentiment..." etc. instead of a static
        # "loading..." for the full 15-30 s window. Asyncio.Queue +
        # sentinel pattern: gather runs as a task; we drain the queue
        # in this generator and yield ctx_progress events; gather's
        # finally puts None as a "done" marker so we exit the loop.
        #
        # Queue payload is `dict | None`: each progress milestone
        # gets wrapped in `{stage, done?, total?}` so the per-symbol
        # news fan-out (C1-3) can carry a counter, while the simpler
        # "phase started" events stay as `{stage}` only. `None` is
        # the sentinel marking gather completion.
        progress_q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _emit_progress(
            stage: str,
            *,
            done: int | None = None,
            total: int | None = None,
        ) -> None:
            payload: dict[str, Any] = {"stage": stage}
            if done is not None:
                payload["done"] = done
            if total is not None:
                payload["total"] = total
            await progress_q.put(payload)

        async def _gather_then_signal() -> dict[str, Any]:
            try:
                return await gather_market_context(
                    db,
                    market=discussion.market,
                    focus_symbols=focus,
                    owner_id=discussion.owner_id,
                    exclude_discussion_id=discussion.id,
                    as_of=discussion.as_of_date,
                    progress_cb=_emit_progress,
                    topic=discussion.topic,
                )
            finally:
                # Sentinel — signals to the drainer that no more
                # progress events are coming so it can break out and
                # await the result. Always fires (success or failure)
                # so the caller never deadlocks.
                await progress_q.put(None)

        ctx_task = asyncio.create_task(_gather_then_signal())
        while True:
            payload = await progress_q.get()
            if payload is None:
                break
            # `payload` already carries `{stage, done?, total?}` —
            # forward verbatim so an older frontend that only reads
            # `stage` keeps working while newer ones can render
            # `done / total` when present.
            yield TurnEvent("ctx_progress", payload)
        # Re-raise any exception the gather hit. The sentinel was
        # still fired by the finally block above, so we got here
        # cleanly.
        context = await ctx_task
        # Snapshot the assembled context so re-opening the discussion
        # later can show "what data the personas saw at the time".
        # Failure to persist is non-fatal — we still want the round
        # to proceed and the personas to reply; the missing snapshot
        # just means this round won't be replayable from the archive.
        try:
            await _upsert_round_context(
                db,
                discussion_id=discussion.id,
                round_number=round_number,
                context=context,
            )
        except Exception as exc:
            log.warning(
                "discussion.round.context_snapshot_failed",
                extra={
                    "discussion_id": str(discussion.id),
                    "round": round_number,
                    "error": str(exc),
                },
            )
        yield TurnEvent("context", {"context": context})

        prior_turns = await get_turns(db, discussion_id=discussion.id)
        # Resolve persona timeout via the runtime config service so admins
        # can retune it from the UI without redeploying. Falls back to
        # the compiled-in setting on any resolver failure.
        try:
            from services.runtime_config_service import get_int as _get_int
            persona_timeout = await _get_int(db, "DISCUSSION_PERSONA_TIMEOUT_SECONDS")
        except Exception:
            persona_timeout = settings.DISCUSSION_PERSONA_TIMEOUT_SECONDS

        # PR-4c: filter frozen personas out of the roster. When a
        # discussion is spawned by a sweep, look up the parent
        # strategy's `persona_status` and drop any persona that's
        # been frozen (auto-frozen by the underperformer detector
        # or manually via the admin endpoint). Live discussions
        # without a sweep parent stay unfiltered. Frozen personas
        # remain on `discussion.persona_ids` for audit trail — we
        # only remove them from THIS round's runtime roster.
        runtime_persona_ids = list(discussion.persona_ids)
        if discussion.sweep_id is not None:
            try:
                from models.backtest_sweep import BacktestSweep
                from models.discussion_strategy_template import (
                    DiscussionStrategyTemplate,
                )
                from services.persona_status_service import (
                    filter_roster_by_status,
                )
                sweep_row = await db.scalar(
                    select(BacktestSweep).where(
                        BacktestSweep.id == discussion.sweep_id,
                    )
                )
                if sweep_row is not None and sweep_row.strategy_id is not None:
                    tmpl = await db.scalar(
                        select(DiscussionStrategyTemplate).where(
                            DiscussionStrategyTemplate.id == sweep_row.strategy_id,
                        )
                    )
                    if tmpl is not None:
                        runtime_persona_ids = filter_roster_by_status(
                            persona_ids=runtime_persona_ids,
                            persona_status=tmpl.persona_status,
                        )
            except Exception as exc:
                log.warning(
                    "discussion.persona_status_filter_failed",
                    extra={
                        "discussion_id": str(discussion.id),
                        "error": str(exc),
                    },
                )

        # Batch-load persona overrides up front so the per-persona loop
        # doesn't make N round-trips to the persona_overrides table.
        specs_by_id = await _resolve_persona_specs(db, runtime_persona_ids)
        # System-task-level LLM override: when the auto-run scheduler
        # passes (provider, model), every persona in this round is
        # rebuilt to point at that one LLM. Lets admins set a single
        # cheap model for the daily auto-run via SystemTasksCard
        # without having to override each persona individually.
        # PersonasCard overrides on individual personas are ignored
        # for this run only.
        if provider_override and model_override:
            from ai.agents import AgentSpec
            specs_by_id = {
                pid: AgentSpec(
                    name=spec.name,
                    description=spec.description,
                    system_prompt=spec.system_prompt,
                    default_provider=provider_override,
                    default_model=model_override,
                )
                for pid, spec in specs_by_id.items()
            }

        # PR-4c: iterate the FILTERED roster (frozen personas
        # already dropped above) so the round runner doesn't waste
        # a turn slot on someone who's been benched. Existing
        # `specs_by_id` keys match this filtered list 1:1.
        for idx, persona_id in enumerate(runtime_persona_ids):
            spec = specs_by_id.get(persona_id)
            if spec is None:
                # persona_id no longer recognised by ai.agents (shouldn't
                # happen since personas are compiled-in, but defensive)
                yield TurnEvent("error", {
                    "message": f"unknown persona: {persona_id}",
                    "persona_id": persona_id,
                })
                continue

            yield TurnEvent("turn_start", {
                "round": round_number,
                "turn_index": idx,
                "persona_id": persona_id,
                "persona_name": spec.name,
            })

            assembled = ""
            usage_seen: dict[str, int] | None = None
            tool_call_total = 0
            tool_call_breakdown: dict[str, int] = {}
            # Wrap the persona's turn in asyncio.timeout so a single stuck
            # provider can't hang the whole round indefinitely. On timeout
            # we emit an error event, persist a placeholder turn, and
            # proceed to the next persona — same pattern as an LLM error.
            # Filter out `<think>...</think>` content as it streams so
            # reasoning models (deepseek-r1, gpt-o1, qwen-3) don't flash
            # internal monologue across the chat UI. The full unfiltered
            # text is still kept in `assembled` so JSON parsing still
            # sees what the model sent (and `_parse_turn_response` strips
            # think blocks again for safety / persistence).
            think_filter = _ThinkBlockFilter()
            try:
                async with asyncio.timeout(persona_timeout):
                    async for event in _ask_persona(
                        db,
                        spec=spec,
                        persona_id=persona_id,
                        topic=discussion.topic,
                        rules=discussion.rules,
                        context=context,
                        prior_turns=prior_turns,
                        user_id=user_id,
                        user_role=user_role,
                        as_of_date=discussion.as_of_date,
                    ):
                        etype = event.get("type")
                        if etype == "delta":
                            chunk = event.get("text", "")
                            assembled += chunk
                            visible = think_filter.feed(chunk)
                            if visible:
                                yield TurnEvent("delta", {
                                    "round": round_number,
                                    "turn_index": idx,
                                    "persona_id": persona_id,
                                    "text": visible,
                                })
                        elif etype == "tool_call":
                            # Forward through so the SSE consumer can
                            # show "buffett 正在執行 run_dcf" inline, AND
                            # record the per-tool count for billing/debug
                            # observability — without this the round shows
                            # token cost but no signal for "why was this
                            # turn slow / which tool got called repeatedly".
                            tool_name = event.get("name") or "_unknown"
                            tool_call_total += 1
                            tool_call_breakdown[tool_name] = (
                                tool_call_breakdown.get(tool_name, 0) + 1
                            )
                            yield TurnEvent("tool_call", {
                                "round": round_number,
                                "turn_index": idx,
                                "persona_id": persona_id,
                                "id":   event.get("id"),
                                "name": event.get("name"),
                                "args": event.get("args"),
                            })
                        elif etype == "tool_result":
                            yield TurnEvent("tool_result", {
                                "round": round_number,
                                "turn_index": idx,
                                "persona_id": persona_id,
                                "id":       event.get("id"),
                                "name":     event.get("name"),
                                "summary":  event.get("summary", ""),
                                "is_error": event.get("is_error", False),
                            })
                        elif etype == "usage":
                            # **Sum** usage across events instead of
                            # overwriting (PR #216). When the persona's
                            # provider goes through a tool loop —
                            # claude_agent's MCP loop or
                            # _openai_compat_tool_loop's max_turns
                            # iteration — each LLM call emits its
                            # own usage event. The earlier code took
                            # only the LAST one, so a 5-turn tool run
                            # dropped ~80% of the actual prompt token
                            # cost. Now every event is added in.
                            if usage_seen is None:
                                usage_seen = {"prompt_tokens": 0, "completion_tokens": 0}
                            usage_seen["prompt_tokens"] += int(
                                event.get("prompt_tokens", 0)
                            )
                            usage_seen["completion_tokens"] += int(
                                event.get("completion_tokens", 0)
                            )
                        elif etype == "error":
                            yield TurnEvent("error", {
                                "message": event.get("message", "LLM error"),
                                "persona_id": persona_id,
                            })
                            assembled = assembled or "（此輪因 LLM 錯誤未取得回覆）"
                            break
            except TimeoutError:
                log.warning(
                    "discussion.turn.timeout",
                    extra={"persona_id": persona_id, "round": round_number,
                           "timeout_s": persona_timeout},
                )
                yield TurnEvent("error", {
                    "message": f"persona timeout after {persona_timeout}s",
                    "persona_id": persona_id,
                })
                assembled = assembled or f"（此輪因 LLM {persona_timeout}s 內未回覆而中止）"
            except Exception as exc:
                log.exception("discussion.turn.failed",
                              extra={"persona_id": persona_id, "round": round_number})
                yield TurnEvent("error", {
                    "message": str(exc),
                    "persona_id": persona_id,
                })
                assembled = assembled or "（此輪因例外中止）"

            # Whether the stream finished cleanly, errored, or timed out,
            # flush any text the think-filter is still buffering. If the
            # tail is inside an open <think> block it gets dropped (the
            # model never closed the tag, so we never want to show it).
            tail = think_filter.flush()
            if tail:
                yield TurnEvent("delta", {
                    "round": round_number,
                    "turn_index": idx,
                    "persona_id": persona_id,
                    "text": tail,
                })

            stance, content = _parse_turn_response(assembled)
            turn_row = DiscussionTurn(
                discussion_id=discussion.id,
                round=round_number,
                turn_index=idx,
                persona_id=persona_id,
                stance=stance,
                content=content,
                citations=None,
            )
            db.add(turn_row)
            await db.commit()
            prior_turns.append(turn_row)

            # Record the persona's LLM usage. Tagged with the actual
            # persona_id (not "_system:..." like sentiment/synthesizer)
            # so the admin UsageCard breakdown can attribute cost per
            # persona/round. Skipped if the provider didn't emit a
            # usage event (some openai-compat backends don't). Tool
            # counts are written even when zero so a row's NULL-vs-{}
            # distinction means "no breakdown captured" not "no calls".
            if usage_seen is not None:
                from services.llm_usage_service import record_usage
                await record_usage(
                    db,
                    user_id=user_id,
                    provider=spec.default_provider,
                    model=spec.default_model,
                    persona_id=persona_id,
                    prompt_tokens=usage_seen["prompt_tokens"],
                    completion_tokens=usage_seen["completion_tokens"],
                    tool_call_count=tool_call_total,
                    tool_call_breakdown=dict(tool_call_breakdown) or None,
                )

            yield TurnEvent("turn_end", {
                "round": round_number,
                "turn_index": idx,
                "persona_id": persona_id,
                "persona_name": spec.name,
                "stance": stance,
                "content": content,
            })

        yield TurnEvent("round_end", {
            "round": round_number,
            "turn_count": len(discussion.persona_ids),
        })
    finally:
        # Always reset to DRAFT so the next round attempt isn't blocked
        # by the router's "round in progress" guard. Use the same atomic
        # SQL UPDATE pattern as round-start so the reset can't fail
        # silently the way an in-memory mutation can. Wrap in its own
        # try so a reset failure doesn't mask the body exception.
        try:
            now = datetime.now(UTC)
            await db.execute(
                update(Discussion)
                .where(Discussion.id == discussion.id)
                .values(status=STATUS_DRAFT, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            discussion.status = STATUS_DRAFT
            discussion.updated_at = now
            log.info(
                "discussion.round.ended",
                extra={
                    "discussion_id": str(discussion.id),
                    "current_round": discussion.current_round,
                },
            )
        except Exception:
            log.exception(
                "discussion.run_round.status_reset_failed",
                extra={"discussion_id": str(discussion.id)},
            )
