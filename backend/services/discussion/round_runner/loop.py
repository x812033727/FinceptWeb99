"""Per-round SSE orchestrator: ``run_round``.

Drives one full persona-by-persona round — atomic round-counter
increment, market-context gathering (with ctx_progress bridging),
frozen-persona filtering, the B4 interjection work-queue, per-turn
streaming/persistence/usage accounting, and the try-finally status
reset back to DRAFT. Pure move out of the former single-module
``round_runner.py`` (R6 PR1); see the package docstring for the
import-layering rules.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import Discussion, DiscussionTurn
from services.discussion.persona_config import _resolve_persona_specs
from services.discussion.round_runner.turn_exec import (
    _MAX_TURN_ATTEMPTS,
    TurnEvent,
    _ask_persona,
    _ThinkBlockFilter,
)
from services.discussion.symbols import extract_focus_symbols
from services.discussion.turn_parsing import (
    ABSTAIN_STANCE,
    _parse_turn_response,
)

log = logging.getLogger(__name__)


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
      - turn_retry   {round, turn_index, persona_id, reason, attempt}
                     (zero-output timeout/LLM-error being retried;
                     followed by a repeated turn_start for the same
                     turn_index so clients reset per-turn state)
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
        USER_INJECTION_STANCE,
        USER_PERSONA_ID,
        _upsert_round_context,
        drain_interjections,
        gather_market_context,
        get_round_contexts,
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
                    strategy=discussion.auto_run_strategy,
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

        # R6 PR2 (opt-in): when round digests are enabled, load the stored
        # per-round recaps so `_format_history` can render an older round
        # as one digest line instead of a bag of per-turn one-liners. Empty
        # map when the feature is off (the default) or nothing's stored yet,
        # in which case `_ask_persona` sees `round_digests=None` and the
        # history output is byte-identical to before this feature.
        round_digests: dict[int, str] | None = None
        if settings.DISCUSSION_ROUND_DIGEST_ENABLED:
            try:
                digest_rows = await get_round_contexts(
                    db, discussion_id=discussion.id,
                )
                found = {r.round: r.digest for r in digest_rows if r.digest}
                round_digests = found or None
            except Exception:
                log.warning(
                    "discussion.round_digest.load_failed",
                    extra={"discussion_id": str(discussion.id)},
                )

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
        #
        # B4: the roster runs as a work QUEUE (not a plain enumerate)
        # so mid-round user interjections can be spliced in at turn
        # boundaries. Before each turn we drain the discussion's
        # pending-interjection queue (fed by POST /interject while
        # status=running); each interjection becomes (a) the owner's
        # question persisted as a `user_input` turn and (b) an extra
        # answer turn by the assigned persona — the named
        # `target_persona` when valid, else the moderator default
        # (first persona of the runtime roster). Both turns carry
        # `injected_by_user=True` in the DB and in their SSE events.
        # `turn_index` is a running counter (== enumerate index when
        # nothing is interjected).
        work: deque[tuple[str, dict[str, Any] | None]] = deque(
            (pid, None) for pid in runtime_persona_ids
        )
        turn_idx = 0
        while True:
            # Drain at every turn boundary — including once more after
            # the last roster persona, so a question asked during the
            # final turn still gets answered within this round.
            for inj in reversed(drain_interjections(discussion.id)):
                target = inj.get("target_persona")
                if target not in specs_by_id:
                    target = (
                        runtime_persona_ids[0] if runtime_persona_ids else None
                    )
                if target is None:
                    continue
                work.appendleft((target, inj))
            if not work:
                break
            persona_id, interjection = work.popleft()
            injected = interjection is not None
            spec = specs_by_id.get(persona_id)
            if spec is None:
                # persona_id no longer recognised by ai.agents (shouldn't
                # happen since personas are compiled-in, but defensive)
                yield TurnEvent("error", {
                    "message": f"unknown persona: {persona_id}",
                    "persona_id": persona_id,
                })
                continue

            if injected:
                # Persist + emit the owner's question first so it sits
                # immediately before the answer in the transcript and
                # in `prior_turns` for every later persona. Emitted as
                # a normal `turn_end` (no streaming — the text already
                # exists) so SSE consumers append it like any turn.
                question_turn = DiscussionTurn(
                    discussion_id=discussion.id,
                    round=round_number,
                    turn_index=turn_idx,
                    persona_id=USER_PERSONA_ID,
                    stance=USER_INJECTION_STANCE,
                    content=interjection["question"],
                    citations=None,
                    injected_by_user=True,
                )
                db.add(question_turn)
                await db.commit()
                prior_turns.append(question_turn)
                yield TurnEvent("turn_end", {
                    "round": round_number,
                    "turn_index": turn_idx,
                    "persona_id": USER_PERSONA_ID,
                    "persona_name": "使用者",
                    "stance": USER_INJECTION_STANCE,
                    "content": interjection["question"],
                    "injected_by_user": True,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "tool_call_count": 0,
                    "breakdown": None,
                })
                turn_idx += 1

            turn_start_payload = {
                "round": round_number,
                "turn_index": turn_idx,
                "persona_id": persona_id,
                "persona_name": spec.name,
                "injected_by_user": injected,
            }
            yield TurnEvent("turn_start", turn_start_payload)

            assembled = ""
            placeholder: str | None = None
            usage_seen: dict[str, int] | None = None
            breakdown: dict[str, Any] | None = None
            tool_call_total = 0
            tool_call_breakdown: dict[str, int] = {}
            # Wrap the persona's turn in asyncio.timeout so a single stuck
            # provider can't hang the whole round indefinitely. A zero-
            # output timeout / LLM error gets ONE retry (fresh timeout
            # window) before we give up — the dominant failure mode is
            # llm-gateway peak-hour queueing, which is transient, and
            # a retry is far cheaper than losing the persona's voice
            # for the round. Usage/tool counters deliberately accumulate
            # ACROSS attempts: the first attempt's tokens were spent.
            # Filter out `<think>...</think>` content as it streams so
            # reasoning models (deepseek-r1, gpt-o1, qwen-3) don't flash
            # internal monologue across the chat UI. The full unfiltered
            # text is still kept in `assembled` so JSON parsing still
            # sees what the model sent (and `_parse_turn_response` strips
            # think blocks again for safety / persistence).
            for attempt in range(1, _MAX_TURN_ATTEMPTS + 1):
                assembled = ""
                placeholder = None
                breakdown = None
                outcome = "ok"
                failure_message = ""
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
                            round_digests=round_digests,
                            interject_question=(
                                interjection["question"] if injected else None
                            ),
                        ):
                            etype = event.get("type")
                            if etype == "delta":
                                chunk = event.get("text", "")
                                assembled += chunk
                                visible = think_filter.feed(chunk)
                                if visible:
                                    yield TurnEvent("delta", {
                                        "round": round_number,
                                        "turn_index": turn_idx,
                                        "persona_id": persona_id,
                                        "text": visible,
                                    })
                            elif etype == "input_breakdown":
                                # Captured (not forwarded as its own SSE
                                # event) — folded into turn_end + persisted
                                # on the DiscussionTurn below.
                                breakdown = event.get("breakdown")
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
                                    "turn_index": turn_idx,
                                    "persona_id": persona_id,
                                    "id":   event.get("id"),
                                    "name": event.get("name"),
                                    "args": event.get("args"),
                                })
                            elif etype == "tool_result":
                                yield TurnEvent("tool_result", {
                                    "round": round_number,
                                    "turn_index": turn_idx,
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
                                outcome = "llm_error"
                                failure_message = event.get("message", "LLM error")
                                placeholder = "（此輪因 LLM 錯誤未取得回覆）"
                                break
                except TimeoutError:
                    log.warning(
                        "discussion.turn.timeout",
                        extra={"persona_id": persona_id, "round": round_number,
                               "timeout_s": persona_timeout, "attempt": attempt},
                    )
                    outcome = "timeout"
                    failure_message = f"persona timeout after {persona_timeout}s"
                    placeholder = f"（此輪因 LLM {persona_timeout}s 內未回覆而中止）"
                except Exception as exc:
                    log.exception("discussion.turn.failed",
                                  extra={"persona_id": persona_id, "round": round_number})
                    outcome = "exception"
                    failure_message = str(exc)
                    placeholder = "（此輪因例外中止）"

                # Whether the stream finished cleanly, errored, or timed out,
                # flush any text the think-filter is still buffering. If the
                # tail is inside an open <think> block it gets dropped (the
                # model never closed the tag, so we never want to show it).
                tail = think_filter.flush()
                if tail:
                    yield TurnEvent("delta", {
                        "round": round_number,
                        "turn_index": turn_idx,
                        "persona_id": persona_id,
                        "text": tail,
                    })

                # Retry only the transient, zero-output failures:
                #   - timeout / LLM error with no deltas → the turn is a
                #     total loss and the cause is usually gateway
                #     congestion, so a second attempt is worth its cost.
                #   - partial text is NOT retried — the salvage parser
                #     keeps the analysis we already paid for.
                #   - a Python exception is NOT retried — same code path
                #     would almost certainly fail the same way.
                retrying = (
                    outcome in ("timeout", "llm_error")
                    and not assembled
                    and attempt < _MAX_TURN_ATTEMPTS
                )
                if outcome != "ok" and not retrying:
                    # Final failure for this turn — only now surface the
                    # soft per-persona error to the stream. Emitting it
                    # on a first attempt that later succeeds would flash
                    # a spurious timeout toast at the user.
                    yield TurnEvent("error", {
                        "message": failure_message,
                        "persona_id": persona_id,
                    })
                if not retrying:
                    break
                log.info(
                    "discussion.turn.retry",
                    extra={"persona_id": persona_id, "round": round_number,
                           "reason": outcome, "attempt": attempt + 1},
                )
                yield TurnEvent("turn_retry", {
                    "round": round_number,
                    "turn_index": turn_idx,
                    "persona_id": persona_id,
                    "reason": outcome,
                    "attempt": attempt + 1,
                })
                # Re-announce the turn so clients reset any per-turn
                # streaming state (buffer, tool-call rows) from the
                # failed attempt. Zero-output guarantees the visible
                # buffer was empty, so there is no flicker.
                yield TurnEvent("turn_start", turn_start_payload)

            if placeholder is not None and not assembled:
                # The persona produced zero output before the failure —
                # there is nothing to parse. Persist the placeholder
                # under the system-only ABSTAIN stance so consensus /
                # stance_distribution treat it as "no response" instead
                # of the parse fallback's supplement (0.5 weight), which
                # silently diluted the room's real agreement level.
                stance, content = ABSTAIN_STANCE, placeholder
            else:
                # Partial text before a timeout still carries real
                # analysis — keep the salvage path (stance + truncated
                # content) rather than discarding paid-for tokens.
                stance, content = _parse_turn_response(assembled)
            turn_row = DiscussionTurn(
                discussion_id=discussion.id,
                round=round_number,
                turn_index=turn_idx,
                persona_id=persona_id,
                stance=stance,
                content=content,
                citations=None,
                input_breakdown=breakdown,
                injected_by_user=injected,
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
                    discussion_id=discussion.id,
                    round=round_number,
                )

            yield TurnEvent("turn_end", {
                "round": round_number,
                "turn_index": turn_idx,
                "persona_id": persona_id,
                "persona_name": spec.name,
                "stance": stance,
                "content": content,
                # Per-persona exact usage (folded in so the UI's "用量明細"
                # updates live, not just at round_end). 0 when the
                # provider emitted no usage event.
                "prompt_tokens": (usage_seen or {}).get("prompt_tokens", 0),
                "completion_tokens": (usage_seen or {}).get("completion_tokens", 0),
                "tool_call_count": tool_call_total,
                "breakdown": breakdown,
                # B4: lets the SSE consumer badge interjection answers
                # AND lets the router's quota accounting skip injected
                # turns (they're charged separately at enqueue time).
                "injected_by_user": injected,
            })
            turn_idx += 1

        # Settle this round's token usage (input + output) for the live
        # per-round tally. All persona rows above were committed per-turn,
        # so the SUM is accurate here. Best-effort: a failure must not
        # break round completion.
        round_tokens: dict[str, int] = {}
        try:
            from services.llm_usage_service import round_usage_totals
            round_tokens = await round_usage_totals(
                db, discussion_id=discussion.id, round=round_number,
            )
        except Exception:
            log.warning(
                "discussion.round.usage_tally_failed",
                extra={"discussion_id": str(discussion.id), "round": round_number},
            )

        yield TurnEvent("round_end", {
            "round": round_number,
            # Actual number of turns persisted this round (roster turns
            # + any interjected question/answer pairs), not the roster
            # size — the two only differ when B4 interjections fired.
            "turn_count": turn_idx,
            "prompt_tokens": round_tokens.get("prompt_tokens", 0),
            "completion_tokens": round_tokens.get("completion_tokens", 0),
            "total_tokens": round_tokens.get("total_tokens", 0),
        })

        # R6 PR2: best-effort round digest (off by default). Summarise THIS
        # round's debate into a compact recap stored on the round-context
        # row. Generation-only — it does NOT change what personas see, so a
        # failure here must never affect the round or its turns; hence the
        # broad guard. Runs after round_end so the SSE stream isn't held up.
        if settings.DISCUSSION_ROUND_DIGEST_ENABLED:
            try:
                from services.discussion.round_digest import (
                    generate_round_digest,
                    store_round_digest,
                )
                this_round = [t for t in prior_turns if t.round == round_number]
                # Bound the aux-model call so a hung provider can't stall the
                # round; timeout is caught by the broad guard below → None.
                async with asyncio.timeout(settings.DISCUSSION_AUX_MODEL_TIMEOUT_S):
                    digest = await generate_round_digest(
                        db,
                        topic=discussion.topic,
                        turns=this_round,
                        user_id=user_id,
                        discussion_id=discussion.id,
                        round_number=round_number,
                    )
                if digest:
                    await store_round_digest(
                        db,
                        discussion_id=discussion.id,
                        round_number=round_number,
                        digest=digest,
                    )
            except Exception:
                log.warning(
                    "discussion.round_digest.pipeline_failed",
                    extra={
                        "discussion_id": str(discussion.id),
                        "round": round_number,
                    },
                )
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
