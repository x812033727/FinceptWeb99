"""Characterization tests freezing ai.llm_router's three provider
tables before the R1 registry refactor:

  1. `stream_chat`'s if/elif dispatch  → which `_*_stream` runs
  2. `default_model_for`               → per-provider default model
  3. `_resolve_api_key`'s attr_map     → settings fallback attribute

The refactor collapses these into one `PROVIDERS` registry; these tests
must pass UNCHANGED before and after. Every mapping asserted here is
the current observed behavior, not an aspiration.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import ai.llm_router as lr

# provider → (stream fn name, expected default model attr or literal)
DISPATCH_TABLE = [
    ("openai", "_openai_stream", "gpt-4o-mini"),
    ("anthropic", "_anthropic_stream", "claude-haiku-4-5-20251001"),
    ("gemini", "_gemini_stream", "gemini-2.0-flash"),
    ("ollama", "_ollama_stream", "llama3.2"),
    ("minimax", "_minimax_stream", lr.settings.MINIMAX_MODEL),
    ("groq", "_groq_stream", lr.settings.GROQ_MODEL),
    ("deepseek", "_deepseek_stream", lr.settings.DEEPSEEK_MODEL),
    ("openrouter", "_openrouter_stream", lr.settings.OPENROUTER_MODEL),
    ("claude_agent", "_claude_agent_stream", lr.settings.CLAUDE_AGENT_MODEL),
]

ALL_STREAM_FNS = [row[1] for row in DISPATCH_TABLE]


def _recording_stream(name: str, calls: list):
    async def fake(messages, model, *args, **kwargs):
        calls.append((name, model))
        yield {"type": "delta", "text": "ok"}
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,fn_name,default_model", DISPATCH_TABLE)
async def test_dispatch_routes_to_expected_stream_with_default_model(
    provider, fn_name, default_model
):
    calls: list = []
    patchers = [
        patch.object(lr, name, _recording_stream(name, calls))
        for name in ALL_STREAM_FNS
    ]
    with patch.object(lr, "_resolve_api_key", AsyncMock(return_value="k")):
        for p in patchers:
            p.__enter__()
        try:
            events = [
                ev async for ev in lr.stream_chat(
                    [{"role": "user", "content": "hi"}], provider=provider,
                )
            ]
        finally:
            for p in reversed(patchers):
                p.__exit__(None, None, None)

    assert events == [{"type": "delta", "text": "ok"}]
    assert calls == [(fn_name, default_model)]


@pytest.mark.asyncio
async def test_dispatch_unknown_provider_raises_value_error():
    with patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            async for _ in lr.stream_chat(
                [{"role": "user", "content": "hi"}], provider="not_a_provider",
            ):
                pass


def test_default_model_for_table():
    for provider, _, default_model in DISPATCH_TABLE:
        assert lr.default_model_for(provider) == default_model
    # Unknown provider → empty string (callers treat as "unlabeled").
    assert lr.default_model_for("nope") == ""


@pytest.mark.asyncio
async def test_resolve_api_key_settings_fallback_table():
    """With no DB session, each provider falls back to its settings
    attribute; providers without an attr (ollama / claude_agent /
    unknown) resolve to empty string."""
    attr_map = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "minimax": "MINIMAX_API_KEY",
        "groq": "GROQ_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    for provider, attr in attr_map.items():
        with patch.object(lr.settings, attr, f"env-{provider}-key"):
            assert await lr._resolve_api_key(provider, None) == f"env-{provider}-key"

    for keyless in ("ollama", "claude_agent", "unknown"):
        assert await lr._resolve_api_key(keyless, None) == ""
