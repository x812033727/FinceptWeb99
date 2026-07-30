"""Post-conclusion 追問 (B4 follow-up): ``interject_followup``.

One bounded follow-up Q&A on a CONCLUDED discussion — persists the
owner's question, has one persona answer it as a single extra turn,
records usage. Pure move out of the former single-module
``round_runner.py`` (R6 PR1); see the package docstring for the
import-layering rules.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import Discussion, DiscussionTurn
from services.discussion.persona_config import _resolve_persona_specs
from services.discussion.round_runner.turn_exec import (
    _MAX_TURN_ATTEMPTS,
    _ask_persona,
)
from services.discussion.turn_parsing import _parse_turn_response

log = logging.getLogger(__name__)


# ── post-conclusion 追問 (B4 follow-up) ─────────────────────────────


async def interject_followup(
    db: AsyncSession,
    discussion: Discussion,
    *,
    question: str,
    target_persona: str | None = None,
    user_id: str | None = None,
    user_role: str | None = None,
) -> tuple[DiscussionTurn, DiscussionTurn]:
    """One bounded follow-up Q&A on a CONCLUDED discussion.

    Persists the owner's question as a `user_input` turn, then has a
    single persona (the named `target_persona` when it's on the roster,
    else the first roster persona — the moderator default) answer it as
    ONE extra turn appended to the current round. No new round is
    opened, no market context is re-gathered (the latest persisted
    round-context snapshot is reused — the follow-up is about the
    existing conclusion, not fresh data), and the conclusion is left
    untouched. Returns `(question_turn, answer_turn)`.

    Raises ValueError for state/validation problems (not concluded,
    empty/oversized question, no usable persona); RuntimeError for
    LLM-side failures (timeout, provider error) so the caller can
    refund the quota charge.
    """
    # Lazy imports from `discussion_service` for the same
    # patch-compatibility reasons `run_round` documents above.
    from services.discussion_service import (
        STATUS_DONE,
        get_round_contexts,
        get_turns,
        inject_user_message,
    )

    if discussion.status != STATUS_DONE or not discussion.conclusion:
        raise ValueError(
            "Follow-up interjection requires a concluded discussion",
        )

    roster = list(discussion.persona_ids or [])
    specs_by_id = await _resolve_persona_specs(db, roster)
    persona_id = (
        target_persona
        if target_persona in specs_by_id
        else next((pid for pid in roster if pid in specs_by_id), None)
    )
    if persona_id is None:
        raise ValueError("No persona available to answer the follow-up")
    spec = specs_by_id[persona_id]

    # Persist the question first — it belongs in the transcript, and
    # `inject_user_message` re-validates text bounds + non-running
    # status and handles turn_index races.
    question_turn = await inject_user_message(db, discussion, content=question)

    context: dict[str, Any] = {}
    try:
        ctx_rows = await get_round_contexts(db, discussion_id=discussion.id)
        if ctx_rows:
            context = ctx_rows[-1].context or {}
    except Exception as exc:
        log.warning(
            "discussion.followup.context_snapshot_read_failed",
            extra={"discussion_id": str(discussion.id), "error": str(exc)},
        )

    prior_turns = await get_turns(db, discussion_id=discussion.id)

    try:
        from services.runtime_config_service import get_int as _get_int
        persona_timeout = await _get_int(
            db, "DISCUSSION_PERSONA_TIMEOUT_SECONDS",
        )
    except Exception:
        persona_timeout = settings.DISCUSSION_PERSONA_TIMEOUT_SECONDS

    assembled = ""
    usage_seen: dict[str, int] | None = None
    breakdown: dict[str, Any] | None = None
    tool_call_total = 0
    # One retry on zero-output timeout / LLM error — same policy (and
    # same rationale: transient gateway congestion) as run_round's
    # per-turn loop. The RuntimeError-on-final-failure contract is
    # unchanged so the router still refunds the quota charge.
    # Usage/tool counters accumulate across attempts.
    last_failure: str | None = None
    for attempt in range(1, _MAX_TURN_ATTEMPTS + 1):
        assembled = ""
        breakdown = None
        last_failure = None
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
                    interject_question=question_turn.content,
                ):
                    etype = event.get("type")
                    if etype == "delta":
                        assembled += event.get("text", "")
                    elif etype == "input_breakdown":
                        breakdown = event.get("breakdown")
                    elif etype == "tool_call":
                        tool_call_total += 1
                    elif etype == "usage":
                        if usage_seen is None:
                            usage_seen = {"prompt_tokens": 0, "completion_tokens": 0}
                        usage_seen["prompt_tokens"] += int(
                            event.get("prompt_tokens", 0)
                        )
                        usage_seen["completion_tokens"] += int(
                            event.get("completion_tokens", 0)
                        )
                    elif etype == "error":
                        last_failure = event.get(
                            "message", "LLM error during follow-up",
                        )
                        break
        except TimeoutError:
            last_failure = (
                f"persona timeout after {persona_timeout}s during follow-up"
            )
        if last_failure is None:
            break
        if assembled or attempt >= _MAX_TURN_ATTEMPTS:
            # Partial text is unusual here (single-shot answer) but if
            # the provider died mid-stream twice, or after any text,
            # keep the fail-and-refund contract rather than persisting
            # a broken answer turn.
            raise RuntimeError(last_failure)
        log.info(
            "discussion.followup.retry",
            extra={"persona_id": persona_id,
                   "discussion_id": str(discussion.id),
                   "attempt": attempt + 1},
        )

    stance, content = _parse_turn_response(assembled)

    # Same bounded turn_index-collision retry as `inject_user_message`
    # (unique (discussion_id, round, turn_index) gate from PR #218).
    from sqlalchemy.exc import IntegrityError

    answer_turn: DiscussionTurn | None = None
    last_exc: Exception | None = None
    for _attempt in range(5):
        max_idx = await db.scalar(
            select(DiscussionTurn.turn_index)
            .where(
                DiscussionTurn.discussion_id == discussion.id,
                DiscussionTurn.round == discussion.current_round,
            )
            .order_by(DiscussionTurn.turn_index.desc())
            .limit(1)
        )
        next_idx = (int(max_idx) + 1) if max_idx is not None else 0
        row = DiscussionTurn(
            discussion_id=discussion.id,
            round=discussion.current_round,
            turn_index=next_idx,
            persona_id=persona_id,
            stance=stance,
            content=content,
            citations=None,
            input_breakdown=breakdown,
            injected_by_user=True,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            last_exc = exc
            continue
        await db.refresh(row)
        answer_turn = row
        break
    if answer_turn is None:
        raise RuntimeError(
            f"interject_followup: turn_index collision retried 5x without "
            f"success (last error: {last_exc})"
        )

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
            tool_call_breakdown=None,
            discussion_id=discussion.id,
            round=discussion.current_round,
        )

    return question_turn, answer_turn
