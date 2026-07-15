"""Aggregation tests for `llm_usage_service.usage_summary`.

Pre-PR these were exercised only through the admin/auth API integration
tests; pulling them out gives us tighter coverage on the
`tool_call_count` + `top_tools` aggregation added when UsageCard
started surfacing per-tool counts.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_usage_event import LLMUsageEvent
from services import llm_usage_service as svc
from middleware.metrics import AI_COST_USD_TOTAL, AI_TOKENS_TOTAL, AI_USAGE_EVENTS_TOTAL


def _evt(
    *,
    user_id: uuid.UUID | None = None,
    provider: str = "minimax",
    model: str = "MiniMax-M2.7",
    persona_id: str | None = "buffett",
    prompt: int = 100,
    completion: int = 50,
    cost: float = 0.001,
    tool_call_count: int = 0,
    tool_call_breakdown: dict[str, int] | None = None,
) -> LLMUsageEvent:
    return LLMUsageEvent(
        user_id=user_id,
        provider=provider, model=model, persona_id=persona_id,
        prompt_tokens=prompt, completion_tokens=completion,
        cost_usd=cost,
        tool_call_count=tool_call_count,
        tool_call_breakdown=tool_call_breakdown,
    )


# ── totals + per-provider rollup ─────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_updates_operational_metrics(db_session: AsyncSession):
    labels = ("openai", "gpt-4o-mini")
    events = AI_USAGE_EVENTS_TOTAL.labels(*labels, "recorded")
    prompt = AI_TOKENS_TOTAL.labels(*labels, "prompt")
    completion = AI_TOKENS_TOTAL.labels(*labels, "completion")
    cost = AI_COST_USD_TOTAL.labels(*labels)
    before = tuple(metric._value.get() for metric in (events, prompt, completion, cost))

    await svc.record_usage(
        db_session,
        user_id=None,
        provider=labels[0],
        model=labels[1],
        persona_id="market_analyst",
        prompt_tokens=1000,
        completion_tokens=500,
    )

    after = tuple(metric._value.get() for metric in (events, prompt, completion, cost))
    assert after[0] - before[0] == 1
    assert after[1] - before[1] == 1000
    assert after[2] - before[2] == 500
    assert after[3] - before[3] == pytest.approx(0.00045)


@pytest.mark.asyncio
async def test_summary_sums_tool_call_count_into_total(db_session: AsyncSession):
    db_session.add_all([
        _evt(tool_call_count=2, tool_call_breakdown={"get_quote": 2}),
        _evt(tool_call_count=3, tool_call_breakdown={"get_quote": 1, "run_dcf": 2}),
        _evt(tool_call_count=0),  # tool-less row should still appear in totals
    ])
    await db_session.commit()

    summary = await svc.usage_summary(db_session, range_days=30)

    assert summary.total_tool_calls == 5
    assert summary.total_requests == 3


@pytest.mark.asyncio
async def test_summary_per_provider_carries_tool_call_count(
    db_session: AsyncSession,
):
    db_session.add_all([
        _evt(provider="minimax", tool_call_count=2,
             tool_call_breakdown={"get_quote": 2}),
        _evt(provider="groq", tool_call_count=5,
             tool_call_breakdown={"run_dcf": 5}),
    ])
    await db_session.commit()

    summary = await svc.usage_summary(db_session, range_days=30)

    by_prov = {b.provider: b for b in summary.by_provider}
    assert by_prov["minimax"].tool_call_count == 2
    assert by_prov["groq"].tool_call_count == 5


# ── top_tools aggregation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_top_tools_sums_breakdown_across_events(db_session: AsyncSession):
    db_session.add_all([
        _evt(tool_call_count=3,
             tool_call_breakdown={"get_quote": 2, "run_dcf": 1}),
        _evt(tool_call_count=4,
             tool_call_breakdown={"get_quote": 3, "get_options_chain": 1}),
    ])
    await db_session.commit()

    summary = await svc.usage_summary(db_session, range_days=30)

    by_name = {t["name"]: t["count"] for t in summary.top_tools}
    assert by_name["get_quote"] == 5
    assert by_name["run_dcf"] == 1
    assert by_name["get_options_chain"] == 1


@pytest.mark.asyncio
async def test_top_tools_caps_at_10_and_sorts_desc(db_session: AsyncSession):
    """15 distinct tools → top_tools is truncated to the 10 most-called,
    sorted by count desc. Without the cap a power-user could push 50+
    tools into the response."""
    breakdown = {f"tool_{i}": i + 1 for i in range(15)}
    db_session.add(_evt(tool_call_count=sum(breakdown.values()),
                        tool_call_breakdown=breakdown))
    await db_session.commit()

    summary = await svc.usage_summary(db_session, range_days=30)

    assert len(summary.top_tools) == 10
    # Highest count first.
    counts = [t["count"] for t in summary.top_tools]
    assert counts == sorted(counts, reverse=True)
    # The 5 lowest-count tools (tool_0..tool_4 with counts 1..5) drop off.
    names = {t["name"] for t in summary.top_tools}
    assert "tool_14" in names  # count=15 — survives
    assert "tool_0" not in names  # count=1 — pruned


@pytest.mark.asyncio
async def test_top_tools_skips_null_breakdown(db_session: AsyncSession):
    """Pre-tracking rows have NULL breakdown; loading them as `None`
    must not fault out the aggregator (NULL → skip, not crash)."""
    db_session.add_all([
        _evt(tool_call_count=0, tool_call_breakdown=None),
        _evt(tool_call_count=2, tool_call_breakdown={"get_quote": 2}),
    ])
    await db_session.commit()

    summary = await svc.usage_summary(db_session, range_days=30)

    assert summary.top_tools == [{"name": "get_quote", "count": 2}]


@pytest.mark.asyncio
async def test_top_tools_empty_when_no_tool_calls(db_session: AsyncSession):
    db_session.add(_evt(tool_call_count=0, tool_call_breakdown=None))
    await db_session.commit()

    summary = await svc.usage_summary(db_session, range_days=30)

    assert summary.top_tools == []
    assert summary.total_tool_calls == 0


# ── user scoping ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_top_tools_respect_user_scoping(db_session: AsyncSession):
    """A `user_id`-scoped summary must NOT mix in another user's tool
    calls — same isolation rule as the existing token totals."""
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    # Need User rows so the FK is satisfied even on SQLite where it's
    # enforced when the column is non-null. We avoid that by leaving
    # user_id NULL for the cross-user setup and instead testing the
    # filter via two user_ids tied directly. The model's `user_id`
    # FK is ON DELETE SET NULL with no immediate constraint check
    # at insert time on SQLite (test backend) so this works.
    from models.user import User, UserRole
    db_session.add_all([
        User(id=u1, email=f"u1-{u1}@t.com", hashed_password="x",
             role=UserRole.analyst),
        User(id=u2, email=f"u2-{u2}@t.com", hashed_password="x",
             role=UserRole.analyst),
    ])
    await db_session.commit()

    db_session.add_all([
        _evt(user_id=u1, tool_call_count=3,
             tool_call_breakdown={"get_quote": 3}),
        _evt(user_id=u2, tool_call_count=99,
             tool_call_breakdown={"run_dcf": 99}),
    ])
    await db_session.commit()

    me = await svc.usage_summary(db_session, range_days=30, user_id=u1)
    assert me.total_tool_calls == 3
    assert me.top_tools == [{"name": "get_quote", "count": 3}]

    everyone = await svc.usage_summary(db_session, range_days=30)
    assert everyone.total_tool_calls == 102
