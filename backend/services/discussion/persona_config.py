"""Persona resolution + tool kwargs + per-persona context filtering.

Three concerns colocated because they all decide *what to send a
single persona LLM call*:

  - ``_resolve_persona_specs`` — batch-load admin overrides for the
    entire roster in one DB round-trip and merge with compiled
    defaults. Replaces N per-persona ``get_agent_resolved`` calls
    during a round.
  - ``_build_persona_tool_kwargs`` — return the tool-related kwargs
    to forward to ``stream_chat``. Mirrors the eligibility rules at
    ``/api/ai/chat`` (``claude_agent`` SDK gate, OpenAI-compat
    providers, role-based ``query_user_data`` filter, backtest
    ``as_of_date`` plumbing).
  - ``_filter_context_for_persona`` — project the full
    ``gather_market_context`` payload down to the blocks the persona
    actually uses. Five archetypes (``_MACRO_PROFILE`` /
    ``_VALUE_PROFILE`` / ``_CONTRARIAN_PROFILE`` / ``_QUANT_PROFILE``
    / ``_PORTFOLIO_PROFILE`` + the ``_SHORT_TERM_PROFILE`` derived
    from quant + per-symbol news) cover the 23 personas without
    enumerating each one. Personas absent from the registry fall
    through to ``_QUANT_PROFILE`` (G7-2b, fail-closed) — unreachable
    while the agent registry and profile map stay 1:1, but safe if they
    ever drift.

Lifted out of ``discussion_service`` to make the persona-routing
seam explicit. ``run_round`` calls ``_resolve_persona_specs`` once
per round; ``_ask_persona`` calls ``_build_persona_tool_kwargs`` +
``_filter_context_for_persona`` once per turn. Both still live in
the monolith and resolve via the re-export.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings

if TYPE_CHECKING:
    from ai.agents import AgentSpec

log = logging.getLogger(__name__)


async def _resolve_persona_specs(
    db: AsyncSession, persona_ids: list[str],
) -> dict[str, "AgentSpec"]:
    """Batch-load admin overrides for the entire roster in one DB round-trip,
    then merge with compiled defaults. Replaces N per-persona
    `get_agent_resolved` calls during a round.
    """
    from ai.agents import AgentSpec, get_agent
    from models.persona_override import PersonaOverride

    if not persona_ids:
        return {}
    rows = (await db.execute(
        select(PersonaOverride).where(PersonaOverride.persona_id.in_(persona_ids))
    )).scalars().all()
    overrides = {r.persona_id: r for r in rows}

    out: dict[str, AgentSpec] = {}
    for pid in persona_ids:
        try:
            base = get_agent(pid)
        except ValueError:
            log.warning("discussion.persona.unknown", extra={"persona_id": pid})
            continue
        ov = overrides.get(pid)
        if ov is None:
            out[pid] = base
        else:
            out[pid] = AgentSpec(
                name=base.name,
                description=base.description,
                system_prompt=base.system_prompt,
                default_provider=ov.provider,
                default_model=ov.model,
            )
    return out


# Provider names whose `_openai_compat_tool_loop` carries the same
# OpenAI-style tools=[...] schema. Mirrors `_OPENAI_COMPAT_PROVIDERS`
# in `api/ai_agents/router.py`; kept narrowly here so the discussion
# service doesn't import the chat-router module (which pulls FastAPI
# into the test path for free).
_OPENAI_COMPAT_TOOL_PROVIDERS = ("minimax", "groq", "deepseek", "openrouter")


def _build_persona_tool_kwargs(
    *,
    provider: str,
    user_role: str | None,
    user_id: str | None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """Return the tool-related kwargs to forward to `stream_chat` for
    a single persona turn.

    Mirrors the eligibility rules at `/api/ai/chat`:
      - `claude_agent` provider: when the SDK is importable, build
        the MCP toolset and cap turns at `CLAUDE_AGENT_MAX_TURNS`.
      - OpenAI-compat providers: build the OpenAI-compat toolset.
      - Any other provider, or any SDK gate failing: returns an
        empty dict so the caller falls through to today's plain
        streaming.

    Role-based tool filtering:
      - viewer: gets all 13 public market-data tools BUT NOT
        `query_user_data`. The free tier still benefits from
        get_quote / get_symbol_news / get_options_chain / etc.
        without exposing portfolio rows to the LLM provider.
      - analyst / admin: gets the full 14-tool surface including
        `query_user_data`.

    `as_of_date` (backtest mode): forwarded into both toolset
    builders so the 4 TW chip-flow tools anchor at the historical
    discussion date instead of `today`. Without this plumbing a
    backtest discussion's tool calls would silently fetch live
    chip-flow data — a future-information leak that defeats the
    point of replaying historical anchors.

    Errors building the toolset (SDK import failure, key fetcher
    blowing up) are swallowed + logged so a single tool-config
    issue never breaks the discussion round — the persona just
    gets a tool-less turn and the rest of the round continues.
    """
    if not user_id:
        return {}
    role = (user_role or "").lower()
    include_user_data = role in ("analyst", "admin")

    prov = (provider or "").lower()
    if prov == "claude_agent":
        if not settings.claude_agent_effective_enabled:
            return {}
        try:
            from ai.tools import build_toolset, tool_names
            return {
                "mcp_server":    build_toolset(
                    user_id,
                    as_of_date=as_of_date,
                    include_user_data=include_user_data,
                ),
                "allowed_tools": tool_names(include_user_data=include_user_data),
                "max_turns":     settings.CLAUDE_AGENT_MAX_TURNS,
            }
        except Exception as exc:
            log.warning(
                "discussion.tools.claude_agent.build_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            return {}

    if prov in _OPENAI_COMPAT_TOOL_PROVIDERS:
        try:
            from ai.tools.openai_compat import build_openai_compat_toolset
            schemas, dispatch = build_openai_compat_toolset(
                user_id,
                as_of_date=as_of_date,
                include_user_data=include_user_data,
            )
        except Exception as exc:
            log.warning(
                "discussion.tools.openai_compat.build_failed",
                extra={"user_id": user_id, "provider": prov, "error": str(exc)},
            )
            return {}
        max_turns_attr = {
            "minimax":    "MINIMAX_MAX_TURNS",
            "groq":       "GROQ_MAX_TURNS",
            "deepseek":   "DEEPSEEK_MAX_TURNS",
            "openrouter": "OPENROUTER_MAX_TURNS",
        }[prov]
        return {
            "openai_tool_schemas":  schemas,
            "openai_tool_dispatch": dispatch,
            "max_turns":            getattr(settings, max_turns_attr, 5),
        }

    return {}


# ── per-persona context filtering ──────────────────────────────────
#
# Sending the full `gather_market_context` payload (~12-15 blocks) to
# every persona costs tokens that don't help: a `macro_analyst` doesn't
# benefit from the `risk_warnings` 處置股 list, and `dalio` doesn't
# care about per-symbol `top_revenue_growers`. Worse, more text =
# more attention dilution — weak models start mixing chip-flow data
# into a macro thesis it shouldn't be in.
#
# `_PERSONA_CONTEXT_PROFILES` enumerates the blocks each persona
# actually wants. Personas absent from the registry fall through to
# `_ALL_PERSONA_BLOCKS` (current behaviour — full context). Admin
# overrides on persona provider/model don't affect filtering — the
# filter is keyed on the canonical persona_id so reskinning Buffett's
# LLM doesn't change what data he cares about.
#
# Always-included keys (top of every persona's view) are the metadata
# the prompt template references unconditionally: `market`,
# `captured_at`, and `errors` (so personas can mention "data was
# incomplete" without us having to enumerate per-archetype).

_ALWAYS_INCLUDED_BLOCKS: frozenset[str] = frozenset({
    "market", "captured_at", "errors",
    # `prior_discussions` is meta-context — past consensus on the
    # same symbols matters to every archetype (a value persona
    # should know we said Hold last week; a quant persona should
    # see when their last momentum call was wrong). Cheap because
    # it's per-symbol-overlap-only.
    "prior_discussions",
    # `recent_lessons` carries past post-mortem takeaways for the
    # same market + per focus_symbol — every archetype benefits
    # from "what we got wrong last time" so it's universal. The
    # block is bounded by the runtime LESSONS_PER_*_LIMIT
    # tunables so token cost stays predictable.
    "recent_lessons",
})

# Full block set — used as the fall-through profile and the union the
# filter compares against. Kept in sync manually with the keys
# `gather_market_context` populates.
_ALL_PERSONA_BLOCKS: frozenset[str] = frozenset({
    "top_gainers", "top_losers", "index",
    "news_sentiment", "per_symbol_news_sentiment", "international_sentiment",
    "short_term_signals",
    "top_foreign_buyers", "margin_balance_trend", "top_revenue_growers",
    "active_buybacks", "govt_bank_flow_5d", "risk_warnings",
    "market_institutional_5d", "taifex_positioning",
    "large_trader_positioning",  # large-trader feed plan Task 4: TX top5/top10 concentration
    "single_stock_futures_oi",   # PR #282: per-stock 外資 期貨 net OI shift
    "taiwan_vix",                # PR #283: 台指選擇權波動率指數
    "upcoming_events_calendar",  # PR #284: market-wide 法說 / 除息 calendar
    "broker_concentration",      # PR #285: per-focus-symbol 主力分點
    "overseas_indicators",   # PR #270: SOX/NDX/SPX/DJI/VIX overnight snapshot
    "focus_briefs", "macro", "user_context", "prior_discussions",
    "recent_lessons",
})

# Five archetypes cover the 19 personas without enumerating each one.
# Each profile is the union of "what this persona uses to form a view".
_MACRO_PROFILE = frozenset({
    "index", "macro", "international_sentiment",
    "overseas_indicators",   # PR #270: SOX/NDX/SPX/DJI/VIX — core macro signal
    "taiwan_vix",            # PR #283: TW implied-vol regime
    "top_foreign_buyers", "govt_bank_flow_5d", "market_institutional_5d",
    "taifex_positioning",   # smart-money directional signal — central to macro view
    "large_trader_positioning",  # TX top5/top10 concentration — companion to taifex_positioning
    "news_sentiment", "per_symbol_news_sentiment",
    # PR #219: macro personas need focus_briefs when the topic
    # names specific stocks. "Fed cuts → 2330 受惠" requires seeing
    # 2330's PE / valuation band — without focus_briefs, dalio /
    # macro_analyst can only deliver pure macro views and never
    # ground them in a tradeable name. focus_briefs is empty when
    # focus_symbols is empty, so this is free for non-symbolic
    # topics (no token cost penalty).
    "focus_briefs",
})

_VALUE_PROFILE = frozenset({
    "focus_briefs", "per_symbol_news_sentiment", "top_revenue_growers",
    "active_buybacks", "news_sentiment", "macro",
    "upcoming_events_calendar",  # PR #284: event windows for entry/exit timing
})

_CONTRARIAN_PROFILE = frozenset({
    "top_losers", "risk_warnings", "margin_balance_trend",
    "focus_briefs", "news_sentiment", "per_symbol_news_sentiment",
    "short_term_signals",   # RSI extremes + volume spikes flag reversals
    "taifex_positioning",   # extreme net OI = contrarian setup
    "large_trader_positioning",  # extreme top5/top10 concentration = contrarian setup
    "single_stock_futures_oi",  # PR #282: extreme per-stock futures OI also a contrarian setup
    "taiwan_vix",               # PR #283: 台 VIX > 25 = panic = contrarian setup
    "upcoming_events_calendar", # PR #284: events create dislocation opportunities
    "broker_concentration",     # PR #285: 主力分點 lockstep = setup confirmation/contrarian alarm
    "overseas_indicators",  # PR #270: ^VIX spikes = global panic = contrarian setup
    "macro",
})

_QUANT_PROFILE = frozenset({
    "focus_briefs", "top_gainers", "top_losers",
    "top_foreign_buyers", "market_institutional_5d", "margin_balance_trend",
    "risk_warnings", "news_sentiment", "macro",
    "short_term_signals",   # core Tier-1 quant signals per focus symbol
    "taifex_positioning",   # market-wide directional bias
    "large_trader_positioning",  # TX top5/top10 concentration — smart-money crowding signal
    "single_stock_futures_oi",  # PR #282: per-stock futures smart-money lead
    "taiwan_vix",               # PR #283: TW VIX as a vol-regime filter
    "upcoming_events_calendar", # PR #284: event-risk gating on entries
    "broker_concentration",     # PR #285: per-focus-symbol 主力分點
    "overseas_indicators",  # PR #270: SOX overnight gap is the dominant TW open driver
})

_PORTFOLIO_PROFILE = frozenset({
    "user_context", "focus_briefs", "per_symbol_news_sentiment",
    "short_term_signals",   # short-term entry/exit timing for held names
    "macro", "news_sentiment", "international_sentiment",
    "overseas_indicators",  # PR #270: risk-on/off positioning needs ^VIX read
    "upcoming_events_calendar",  # PR #284: position sizing around earnings
})

# Short-term traders (Livermore / PTJ / Minervini / Raschke) share the
# quant block set but ALSO need per_symbol_news_sentiment — tape reading
# is incomplete without knowing what news is driving the print. Pure
# quants (Simons / Asness) intentionally exclude news; short-term
# discretionary traders rely on it for entry timing.
_SHORT_TERM_PROFILE = _QUANT_PROFILE | frozenset({
    "per_symbol_news_sentiment",
})

_PERSONA_CONTEXT_PROFILES: dict[str, frozenset[str]] = {
    # CFA-style functional
    "market_analyst":    _QUANT_PROFILE,
    "portfolio_advisor": _PORTFOLIO_PROFILE,
    "risk_manager":      _CONTRARIAN_PROFILE | {"user_context"},
    "macro_analyst":     _MACRO_PROFILE,
    "earnings_analyst":  _VALUE_PROFILE,
    "trading_coach":     _QUANT_PROFILE,
    # `claude_research` has tools — give it everything so it has the
    # full picture before deciding which tool to call.
    "claude_research":   _ALL_PERSONA_BLOCKS,

    # Value / quality investors
    "buffett":  _VALUE_PROFILE,
    "graham":   _VALUE_PROFILE,
    "munger":   _VALUE_PROFILE | {"risk_warnings"},
    "lynch":    _VALUE_PROFILE | {"top_gainers"},
    "fisher":   _VALUE_PROFILE,
    "smith":    _VALUE_PROFILE,

    # Contrarian
    "marks":    _CONTRARIAN_PROFILE,
    "klarman":  _CONTRARIAN_PROFILE,
    # Kostolany trades on liquidity + crowd psychology: macro money flow
    # ("資金") + contrarian panic signals (margin balance = weak hands,
    # VIX = fear, news sentiment = crowd "心理").
    "kostolany": _MACRO_PROFILE | _CONTRARIAN_PROFILE,

    # Macro
    "dalio":    _MACRO_PROFILE,
    "soros":    _MACRO_PROFILE | {"top_gainers", "focus_briefs"},

    # Quant
    "simons":   _QUANT_PROFILE,
    "asness":   _QUANT_PROFILE,

    # Short-term traders. PTJ also reads international_sentiment (his
    # macro overlay drives the regime call). Minervini watches buybacks
    # as a leading-stock confirmation signal.
    "livermore":  _SHORT_TERM_PROFILE,
    "ptj":        _SHORT_TERM_PROFILE | {"international_sentiment"},
    "minervini":  _SHORT_TERM_PROFILE | {"active_buybacks"},
    "raschke":    _SHORT_TERM_PROFILE,
}


def _filter_context_for_persona(
    ctx: dict[str, Any], persona_id: str,
) -> dict[str, Any]:
    """Project `ctx` down to the blocks `persona_id` actually uses,
    plus the always-included metadata keys.

    Personas not in `_PERSONA_CONTEXT_PROFILES` fall back to the
    `_QUANT_PROFILE` (G7-2b, fail-CLOSED). Every compiled-in agent has a
    profile — the registry and the profile map are 1:1 — so this branch
    is unreachable in normal operation; it only fires if the two ever
    drift (a new agent added without a profile). Fail-closed means such a
    drift shows up as a slightly-too-narrow (QUANT) context — cheaper and
    it can never leak an uncatalogued diagnostic block (e.g.
    `news_backfill`) to a persona — instead of the old fail-open, which
    silently sent the full 30-50KB ctx to something we hadn't vetted. The
    warning makes the drift visible so the profile map gets fixed.
    """
    profile = _PERSONA_CONTEXT_PROFILES.get(persona_id)
    if profile is None:
        log.warning(
            "discussion.persona.no_profile_fail_closed",
            extra={"persona_id": persona_id},
        )
        profile = _QUANT_PROFILE
    allowed = _ALWAYS_INCLUDED_BLOCKS | profile
    return {k: v for k, v in ctx.items() if k in allowed}
