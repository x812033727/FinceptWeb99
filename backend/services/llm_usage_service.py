"""LLM usage tracking — record per-request token counts + cost.

Approach: each chat request that completes successfully calls
`record_usage(...)` with the token counts the provider returned in its
streaming `usage` chunk. Cost is computed via `RATE_TABLE` (USD per 1K
tokens, separate input/output rates). Rates are best-effort — operators
should keep them in sync with provider pricing changes.

Aggregation: `monthly_summary` returns aggregated cost + tokens for the
last N months, optionally scoped to one user. AdminPage displays
system-wide aggregates; SettingsPage shows the caller's own.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_usage_event import LLMUsageEvent

logger = logging.getLogger(__name__)


# USD per 1K tokens. Format: (provider, model_substring) → (input_rate, output_rate).
# Match is "best prefix" — first key that the model name starts with wins.
# Rates as of 2026-Q2; operators should adjust as pricing changes.
RATE_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    # OpenAI
    ("openai", "gpt-4o-mini"):     (0.00015, 0.0006),
    ("openai", "gpt-4o"):          (0.0025,  0.01),
    ("openai", "gpt-4-turbo"):     (0.01,    0.03),
    ("openai", "gpt-3.5-turbo"):   (0.0005,  0.0015),
    # Anthropic
    ("anthropic", "claude-haiku"):  (0.0008,  0.004),
    ("anthropic", "claude-sonnet"): (0.003,   0.015),
    ("anthropic", "claude-opus"):   (0.015,   0.075),
    # Gemini
    ("gemini", "gemini-2.0-flash"): (0.0001,  0.0004),
    ("gemini", "gemini-1.5-flash"): (0.00007, 0.0003),
    ("gemini", "gemini-1.5-pro"):   (0.00125, 0.005),
    # MiniMax
    ("minimax", "MiniMax-M3"):      (0.0007,  0.0028),
    ("minimax", "MiniMax-M2.7"):    (0.001,   0.005),
    ("minimax", "abab6.5s"):        (0.00007, 0.00007),
    ("minimax", "abab6.5"):         (0.0004,  0.0004),
    ("minimax", "MiniMax-Text-01"): (0.0008,  0.004),
    # Groq (fast hosting; rates approximate)
    ("groq", "llama-3.3-70b"):     (0.00059, 0.00079),
    ("groq", "llama-3.1-70b"):     (0.00059, 0.00079),
    ("groq", "mixtral-8x7b"):      (0.00024, 0.00024),
    # DeepSeek
    ("deepseek", "deepseek-chat"):     (0.00027, 0.00110),
    ("deepseek", "deepseek-reasoner"): (0.00055, 0.00219),
    # OpenRouter — varies wildly by underlying model; default to a middle-cost
    # estimate. Operators wanting exact accounting should consult OpenRouter's
    # billing dashboard rather than this estimate.
    ("openrouter", ""):            (0.001, 0.003),
}

_DEFAULT_RATE = (0.001, 0.003)


def estimate_cost_usd(provider: str, model: str, prompt_tok: int, completion_tok: int) -> float:
    """Cheap dict lookup — best matching (provider, model_prefix)."""
    candidates = [
        (mprefix, rates) for (p, mprefix), rates in RATE_TABLE.items() if p == provider
    ]
    in_rate, out_rate = _DEFAULT_RATE
    best_len = -1
    for prefix, rates in candidates:
        if model.startswith(prefix) and len(prefix) > best_len:
            in_rate, out_rate = rates
            best_len = len(prefix)
    return (prompt_tok / 1000.0) * in_rate + (completion_tok / 1000.0) * out_rate


async def record_usage(
    db: AsyncSession,
    *,
    user_id: str | None,
    provider: str,
    model: str,
    persona_id: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    tool_call_count: int = 0,
    tool_call_breakdown: dict[str, int] | None = None,
    discussion_id: uuid.UUID | None = None,
    round: int | None = None,
) -> None:
    """Insert one usage row. Failures are logged, never raised — usage
    accounting must not fail a successful chat response.

    `tool_call_count` / `tool_call_breakdown` are optional — pre-tool-loop
    callers (Anthropic / Gemini direct chat, news_sentiment scorer) pass
    nothing and the row records 0 / NULL, matching pre-PR behaviour.

    `discussion_id` / `round` tag the row for per-round token tallies
    (the discussion orchestrator passes both for persona turns; the
    synthesizer passes `discussion_id` with `round=None`). Non-discussion
    callers pass neither and the row records NULL / NULL.
    """
    try:
        cost = estimate_cost_usd(provider, model, prompt_tokens, completion_tokens)
        uid = uuid.UUID(user_id) if user_id else None
        evt = LLMUsageEvent(
            user_id=uid,
            provider=provider,
            model=model,
            persona_id=persona_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=Decimal(f"{cost:.6f}"),
            tool_call_count=tool_call_count,
            tool_call_breakdown=dict(tool_call_breakdown) if tool_call_breakdown else None,
            discussion_id=discussion_id,
            round=round,
        )
        db.add(evt)
        await db.commit()
        from middleware.metrics import (
            AI_COST_USD_TOTAL,
            AI_TOKENS_TOTAL,
            AI_USAGE_EVENTS_TOTAL,
        )
        AI_USAGE_EVENTS_TOTAL.labels(provider, model, "recorded").inc()
        AI_TOKENS_TOTAL.labels(provider, model, "prompt").inc(prompt_tokens)
        AI_TOKENS_TOTAL.labels(provider, model, "completion").inc(completion_tokens)
        AI_COST_USD_TOTAL.labels(provider, model).inc(cost)
    except Exception as exc:  # noqa: BLE001
        from middleware.metrics import AI_USAGE_EVENTS_TOTAL
        AI_USAGE_EVENTS_TOTAL.labels(provider, model, "failed").inc()
        logger.warning("llm_usage.record_failed", extra={
            "provider": provider, "model": model, "error": str(exc),
        })


# ── Aggregation ──────────────────────────────────────────────────

@dataclass
class UsageBucket:
    period: str        # "2026-04" for monthly
    provider: str
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    tool_call_count: int = 0


@dataclass
class UsageSummary:
    range_days: int
    user_scoped: bool
    total_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float
    by_provider: list[UsageBucket]    # one bucket per provider
    by_day: list[dict[str, Any]]      # [{date: 'YYYY-MM-DD', cost_usd: float, requests: int}]
    total_tool_calls: int = 0
    top_tools: list[dict[str, Any]] = field(default_factory=list)
    # ↑ [{name: str, count: int}] sorted desc by count, capped at 10.
    # Aggregated from tool_call_breakdown JSON across the window so the
    # admin can see which tools dominate persona behaviour without
    # parsing per-row JSON in the UI.


async def usage_summary(
    db: AsyncSession,
    *,
    range_days: int = 30,
    user_id: uuid.UUID | None = None,
) -> UsageSummary:
    """Aggregate usage over the last `range_days` days. user_id=None = all users."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=range_days)

    base = select(LLMUsageEvent).where(LLMUsageEvent.created_at >= cutoff)
    if user_id is not None:
        base = base.where(LLMUsageEvent.user_id == user_id)

    # Totals
    totals_q = select(
        func.count(LLMUsageEvent.id),
        func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
        func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
        func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0),
        func.coalesce(func.sum(LLMUsageEvent.tool_call_count), 0),
    ).where(LLMUsageEvent.created_at >= cutoff)
    if user_id is not None:
        totals_q = totals_q.where(LLMUsageEvent.user_id == user_id)
    totals_row = (await db.execute(totals_q)).one()

    # By provider/model
    bp_q = select(
        LLMUsageEvent.provider,
        LLMUsageEvent.model,
        func.count(LLMUsageEvent.id),
        func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
        func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
        func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0),
        func.coalesce(func.sum(LLMUsageEvent.tool_call_count), 0),
    ).where(LLMUsageEvent.created_at >= cutoff).group_by(
        LLMUsageEvent.provider, LLMUsageEvent.model,
    ).order_by(func.sum(LLMUsageEvent.cost_usd).desc())
    if user_id is not None:
        bp_q = bp_q.where(LLMUsageEvent.user_id == user_id)
    bp_rows = (await db.execute(bp_q)).all()
    by_provider = [
        UsageBucket(
            period=f"last_{range_days}d",
            provider=row[0], model=row[1], requests=row[2],
            prompt_tokens=int(row[3]), completion_tokens=int(row[4]),
            cost_usd=float(row[5]),
            tool_call_count=int(row[6]),
        )
        for row in bp_rows
    ]

    # Top tools — aggregated from `tool_call_breakdown` JSON across
    # every event in the window where the breakdown was captured. We
    # do the merge in Python because portable JSON aggregation across
    # SQLite (test) + Postgres (prod) requires per-dialect functions
    # we'd rather avoid; only events with tool_call_count > 0 are
    # loaded so a vanilla provider's millions of tool-less rows don't
    # land in memory.
    tt_q = select(LLMUsageEvent.tool_call_breakdown).where(
        LLMUsageEvent.created_at >= cutoff,
        LLMUsageEvent.tool_call_count > 0,
        LLMUsageEvent.tool_call_breakdown.is_not(None),
    )
    if user_id is not None:
        tt_q = tt_q.where(LLMUsageEvent.user_id == user_id)
    tool_totals: dict[str, int] = {}
    for (breakdown,) in (await db.execute(tt_q)).all():
        if not isinstance(breakdown, dict):
            continue
        for name, n in breakdown.items():
            try:
                tool_totals[str(name)] = tool_totals.get(str(name), 0) + int(n)
            except (TypeError, ValueError):
                continue
    top_tools = [
        {"name": name, "count": count}
        for name, count in sorted(
            tool_totals.items(), key=lambda kv: kv[1], reverse=True,
        )[:10]
    ]

    # By day — for the chart line
    bd_q = select(
        func.date(LLMUsageEvent.created_at).label("day"),
        func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0),
        func.count(LLMUsageEvent.id),
    ).where(LLMUsageEvent.created_at >= cutoff).group_by("day").order_by("day")
    if user_id is not None:
        bd_q = bd_q.where(LLMUsageEvent.user_id == user_id)
    bd_rows = (await db.execute(bd_q)).all()
    by_day = [
        {
            "date": str(row[0]),
            "cost_usd": float(row[1]),
            "requests": int(row[2]),
        }
        for row in bd_rows
    ]

    return UsageSummary(
        range_days=range_days,
        user_scoped=user_id is not None,
        total_requests=int(totals_row[0]),
        total_prompt_tokens=int(totals_row[1]),
        total_completion_tokens=int(totals_row[2]),
        total_cost_usd=float(totals_row[3]),
        by_provider=by_provider,
        by_day=by_day,
        total_tool_calls=int(totals_row[4]),
        top_tools=top_tools,
    )


# ── Per-round token tally (每輪結算給 AI 的 token) ──────────────────

async def round_usage_totals(
    db: AsyncSession,
    *,
    discussion_id: uuid.UUID,
    round: int,
) -> dict[str, int]:
    """Sum input + output tokens recorded for one discussion round.

    Called at `round_end` (all persona rows for the round are already
    committed). Excludes the synthesizer row (round IS NULL). Returns
    zeros when no rows match (e.g. provider emitted no usage)."""
    stmt = select(
        func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
        func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
        func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0),
    ).where(
        LLMUsageEvent.discussion_id == discussion_id,
        LLMUsageEvent.round == round,
    )
    row = (await db.execute(stmt)).one()
    prompt, completion = int(row[0]), int(row[1])
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost_usd": float(row[2]),
    }


async def discussion_round_usage(
    db: AsyncSession,
    *,
    discussion_id: uuid.UUID,
) -> list[dict[str, int]]:
    """Per-round token totals for a whole discussion, ordered by round.

    Backs GET /sessions/{id}/round-usage so a reopened discussion shows
    each round's tally. Synthesizer rows (round IS NULL) are excluded."""
    stmt = (
        select(
            LLMUsageEvent.round,
            func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0),
        )
        .where(
            LLMUsageEvent.discussion_id == discussion_id,
            LLMUsageEvent.round.isnot(None),
        )
        .group_by(LLMUsageEvent.round)
        .order_by(LLMUsageEvent.round)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "round": int(r[0]),
            "prompt_tokens": int(r[1]),
            "completion_tokens": int(r[2]),
            "total_tokens": int(r[1]) + int(r[2]),
            "cost_usd": float(r[3]),
        }
        for r in rows
    ]


async def discussion_round_persona_usage(
    db: AsyncSession,
    *,
    discussion_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Per-(round, persona) exact token / cost / tool counts for a whole
    discussion, ordered by (round, persona).

    Backs GET /sessions/{id}/round-usage/detail — the finer breakdown
    sitting under each round's total. Synthesizer rows (round IS NULL)
    are excluded. The per-section / per-block prompt composition lives on
    `discussion_turns.input_breakdown` and is joined in by the router."""
    stmt = (
        select(
            LLMUsageEvent.round,
            LLMUsageEvent.persona_id,
            LLMUsageEvent.provider,
            LLMUsageEvent.model,
            func.coalesce(func.sum(LLMUsageEvent.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0),
            func.coalesce(func.sum(LLMUsageEvent.tool_call_count), 0),
        )
        .where(
            LLMUsageEvent.discussion_id == discussion_id,
            LLMUsageEvent.round.isnot(None),
        )
        .group_by(
            LLMUsageEvent.round,
            LLMUsageEvent.persona_id,
            LLMUsageEvent.provider,
            LLMUsageEvent.model,
        )
        .order_by(LLMUsageEvent.round, LLMUsageEvent.persona_id)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "round": int(r[0]),
            "persona_id": r[1],
            "provider": r[2],
            "model": r[3],
            "prompt_tokens": int(r[4]),
            "completion_tokens": int(r[5]),
            "total_tokens": int(r[4]) + int(r[5]),
            "cost_usd": float(r[6]),
            "tool_call_count": int(r[7]),
        }
        for r in rows
    ]
