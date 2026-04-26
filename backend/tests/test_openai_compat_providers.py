"""Smoke tests for the three OpenAI-compat providers added on top of the
existing MiniMax wiring: Groq, DeepSeek, OpenRouter.

Deep streaming + tool-loop semantics are already exercised by
`test_minimax_provider.py` against the same `_openai_compat_tool_loop`
helper. These tests verify the per-provider wiring (correct host, chat
path, headers) and the `stream_chat` dispatch routing.
"""
from __future__ import annotations

import json

import pytest

from ai import llm_router as router


# ── shared fakes ──────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, lines: list[str], status_code: int = 200):
        self.lines = lines
        self.status_code = status_code

    async def aread(self) -> bytes:
        return "".join(self.lines).encode()

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _StreamCtx:
    def __init__(self, resp: _FakeResp, captured: dict):
        self._resp = resp
        self._captured = captured

    async def __aenter__(self) -> _FakeResp:
        return self._resp

    async def __aexit__(self, *a):
        return None


def _capturing_client_factory(resp: _FakeResp, captured: dict):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def stream(self, method: str, url: str, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["payload"] = json
            return _StreamCtx(resp, captured)

    return _Client


def _ok_lines() -> list[str]:
    return [
        f"data: {json.dumps({'choices':[{'delta':{'content':'hi'},'finish_reason':None}]})}\n",
        f"data: {json.dumps({'choices':[{'delta':{},'finish_reason':'stop'}]})}\n",
        "data: [DONE]\n",
    ]


# ── Groq ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_groq_hits_correct_endpoint(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(router.httpx, "AsyncClient",
                        _capturing_client_factory(_FakeResp(_ok_lines()), captured))
    monkeypatch.setattr(router.settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(router.settings, "GROQ_HOST", "https://api.groq.com/openai")

    events = [
        ev async for ev in router._groq_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="llama-3.3-70b-versatile",
            max_tokens=64, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert events == [{"type": "delta", "text": "hi"}]
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["payload"]["model"] == "llama-3.3-70b-versatile"
    assert "tools" not in captured["payload"]  # no tools passed → no tools field


# ── DeepSeek ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_hits_correct_endpoint(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(router.httpx, "AsyncClient",
                        _capturing_client_factory(_FakeResp(_ok_lines()), captured))
    monkeypatch.setattr(router.settings, "DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(router.settings, "DEEPSEEK_HOST", "https://api.deepseek.com")

    events = [
        ev async for ev in router._deepseek_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="deepseek-chat",
            max_tokens=64, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert events == [{"type": "delta", "text": "hi"}]
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["payload"]["model"] == "deepseek-chat"


# ── OpenRouter ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openrouter_hits_correct_endpoint_and_optional_headers(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(router.httpx, "AsyncClient",
                        _capturing_client_factory(_FakeResp(_ok_lines()), captured))
    monkeypatch.setattr(router.settings, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(router.settings, "OPENROUTER_HOST", "https://openrouter.ai/api")
    monkeypatch.setattr(router.settings, "OPENROUTER_REFERER", "https://example.com")
    monkeypatch.setattr(router.settings, "OPENROUTER_TITLE", "FinceptWeb")

    events = [
        ev async for ev in router._openrouter_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-3.5-sonnet",
            max_tokens=64, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert events == [{"type": "delta", "text": "hi"}]
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["HTTP-Referer"] == "https://example.com"
    assert captured["headers"]["X-Title"] == "FinceptWeb"
    assert captured["payload"]["model"] == "anthropic/claude-3.5-sonnet"


@pytest.mark.asyncio
async def test_openrouter_skips_optional_headers_when_empty(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(router.httpx, "AsyncClient",
                        _capturing_client_factory(_FakeResp(_ok_lines()), captured))
    monkeypatch.setattr(router.settings, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(router.settings, "OPENROUTER_REFERER", "")
    monkeypatch.setattr(router.settings, "OPENROUTER_TITLE", "")

    [
        ev async for ev in router._openrouter_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="openai/gpt-4o-mini",
            max_tokens=64, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert "HTTP-Referer" not in captured["headers"]
    assert "X-Title" not in captured["headers"]


# ── stream_chat dispatch routing ──────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider,fn_name,model_setting,default_model", [
    ("groq", "_groq_stream", "GROQ_MODEL", "llama-3.3-70b-versatile"),
    ("deepseek", "_deepseek_stream", "DEEPSEEK_MODEL", "deepseek-chat"),
    ("openrouter", "_openrouter_stream", "OPENROUTER_MODEL", "openai/gpt-4o-mini"),
])
async def test_stream_chat_routes_to_correct_provider(
    monkeypatch, provider, fn_name, model_setting, default_model,
):
    captured: dict = {}

    async def fake_stream(messages, model, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        yield {"type": "delta", "text": f"{provider}:{model}"}

    monkeypatch.setattr(router, fn_name, fake_stream)
    monkeypatch.setattr(router.settings, model_setting, default_model)

    events = [
        ev async for ev in router.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            provider=provider,
            model=None,
        )
    ]
    assert events == [{"type": "delta", "text": f"{provider}:{default_model}"}]
    assert captured["model"] == default_model
