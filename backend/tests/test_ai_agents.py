"""Inventory + sanity tests for the agent persona catalogue.

The 19 personas (7 functional + 12 legendary investors) are pure data —
these tests guard against accidental drops, broken provider/model
defaults, and empty system prompts.
"""
from __future__ import annotations

import pytest

from ai import agents as agents_module
from ai.agents import AgentSpec, get_agent, list_agents


_VALID_PROVIDERS = {
    "openai", "anthropic", "gemini", "ollama", "minimax",
    "groq", "deepseek", "openrouter", "claude_agent",
}

_FUNCTIONAL_IDS = {
    "market_analyst", "portfolio_advisor", "risk_manager",
    "macro_analyst", "earnings_analyst", "trading_coach",
    "claude_research",
}

_MASTER_IDS = {
    "buffett", "graham", "munger",            # value
    "lynch", "fisher", "smith",               # quality-growth
    "marks", "klarman",                       # contrarian
    "dalio", "soros",                         # macro
    "simons", "asness",                       # quant
}


def test_total_agent_count_is_19():
    listed = list_agents()
    assert len(listed) == 19, f"expected 19 agents, got {len(listed)}: {[a['id'] for a in listed]}"


def test_all_expected_agent_ids_present():
    ids = {a["id"] for a in list_agents()}
    missing = (_FUNCTIONAL_IDS | _MASTER_IDS) - ids
    extra = ids - (_FUNCTIONAL_IDS | _MASTER_IDS)
    assert not missing, f"missing personas: {missing}"
    assert not extra, f"unexpected personas: {extra}"


def test_get_agent_returns_each_master_persona():
    for pid in _MASTER_IDS:
        spec = get_agent(pid)
        assert isinstance(spec, AgentSpec)
        assert spec.name, f"{pid} has empty name"
        assert spec.description, f"{pid} has empty description"


def test_get_agent_rejects_unknown_id():
    with pytest.raises(ValueError, match="Unknown agent"):
        get_agent("not_a_real_persona")


@pytest.mark.parametrize("aid", sorted(_FUNCTIONAL_IDS | _MASTER_IDS))
def test_persona_system_prompt_is_substantial(aid):
    spec = get_agent(aid)
    assert spec.system_prompt and spec.system_prompt.strip(), f"{aid} has empty system_prompt"
    # Master personas should be richer than the trivial one-liner;
    # require at least 200 chars so a future accidental truncation gets caught.
    assert len(spec.system_prompt) >= 200, (
        f"{aid} system_prompt suspiciously short: {len(spec.system_prompt)} chars"
    )


@pytest.mark.parametrize("aid", sorted(_FUNCTIONAL_IDS | _MASTER_IDS))
def test_persona_default_provider_is_known(aid):
    spec = get_agent(aid)
    assert spec.default_provider in _VALID_PROVIDERS, (
        f"{aid} uses unknown provider {spec.default_provider!r}"
    )
    assert spec.default_model, f"{aid} has empty default_model"


def test_master_persona_prompts_mention_their_signature_concepts():
    """Lightweight smoke check: each master's prompt should at least name-drop
    a concept they're famous for. Catches accidental copy-paste collisions
    where two personas end up with the same prompt body."""
    spec_keywords = {
        "buffett": ["moat"],
        "graham": ["margin of safety"],
        "munger": ["invert"],
        "lynch": ["tenbagger"],
        "fisher": ["scuttlebutt"],
        "smith": ["roce", "compounder"],
        "marks": ["second-level", "cycle"],
        "klarman": ["margin of safety", "asymmetric"],
        "dalio": ["all-weather", "debt cycle"],
        "soros": ["reflexiv"],
        "simons": ["statistical", "factor"],
        "asness": ["factor", "momentum"],
    }
    for pid, keywords in spec_keywords.items():
        prompt = get_agent(pid).system_prompt.lower()
        hit = any(kw in prompt for kw in keywords)
        assert hit, f"{pid} prompt is missing all signature keywords {keywords}"


def test_internal_dict_matches_list_agents():
    """Belt and suspenders: the dict and the list helper should agree."""
    listed_ids = {a["id"] for a in list_agents()}
    dict_ids = set(agents_module._AGENTS.keys())
    assert listed_ids == dict_ids
