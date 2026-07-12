"""Role-based model registry — the single place that answers
"which (provider, model) should THIS role use by default?".

Today the same model literals are repeated ~30× across `ai/agents.py`
(23 personas), `services/system_task_config_service.py` (3 tasks),
`config.py` and two validators. This module is the R1 skeleton those
callers converge on in R4, when the subscription stack (gateway
providers) becomes selectable per role.

Resolution order at the call sites stays unchanged: explicit request >
DB override (persona_override / system_task_config) > THIS registry.
The registry itself is stack-aware: `AI_DEFAULT_STACK=api` (current
default) keeps today's API-key defaults; `subscription` re-points roles
at the gateway/subscription providers (entries land in R4 — until then
both stacks resolve identically so adopting the registry is a pure
refactor).

Roles are coarse workload classes, not individual personas:
  - "persona.functional"   the 7 CFA-style analyst personas
  - "persona.legendary"    the 16 legendary-investor personas
  - "persona.research"     the tool-using deep-research persona
  - "synthesizer"          discussion conclusion synthesis
  - "stock_report"         AI stock report
  - "portfolio_review"     AI portfolio review
  - "news_sentiment"       headline sentiment batch scoring
  - "lessons"              post-mortem lesson extraction
"""
from __future__ import annotations

from dataclasses import dataclass

from config import settings


@dataclass(frozen=True)
class ModelAssignment:
    provider: str
    model: str
    # Cost/capability tier the assignment was chosen for — "premium"
    # (best reasoning), "standard", or "economy" (bulk calls). Purely
    # informational today; R4's tiering reads it when rebalancing
    # workloads across subscription quotas.
    tier: str


# Current-production defaults (the "api" stack) — mirrors what the
# call sites hardcode today so adoption changes nothing observable.
_API_STACK: dict[str, ModelAssignment] = {
    "persona.functional": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "standard"),
    "persona.legendary": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "economy"),
    "persona.research": ModelAssignment("claude_agent", settings.CLAUDE_AGENT_MODEL, "premium"),
    "synthesizer": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "standard"),
    "stock_report": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "standard"),
    "portfolio_review": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "standard"),
    "news_sentiment": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "economy"),
    "lessons": ModelAssignment("anthropic", "claude-haiku-4-5-20251001", "economy"),
}

# Subscription stack — populated in R4 (gateway providers claude_sub /
# codex_sub / agy + minimax tiering). Falls back to the API stack for
# any role not yet re-pointed, so flipping AI_DEFAULT_STACK early is
# safe.
_SUBSCRIPTION_STACK: dict[str, ModelAssignment] = {}

_STACKS = {
    "api": _API_STACK,
    "subscription": _SUBSCRIPTION_STACK,
}


def resolve_role(role: str, stack: str | None = None) -> ModelAssignment | None:
    """Default (provider, model, tier) for a workload role, or None for
    unknown roles (caller falls back to its own legacy default — this
    keeps registry adoption incremental)."""
    stack_name = (stack or getattr(settings, "AI_DEFAULT_STACK", "api")).lower()
    chosen = _STACKS.get(stack_name, _API_STACK)
    if role in chosen:
        return chosen[role]
    # Subscription stack is sparse until R4 — fall through to api.
    return _API_STACK.get(role)


def known_roles() -> list[str]:
    return sorted(_API_STACK)
