"""LLM gateway provider (claude_sub / codex_sub / agy) — SSE parsing,
success streaming, and the fail-before-output → API fallback path.

The gateway HTTP layer is mocked; no host sidecar is required.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

import ai.llm_router as lr


class _FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"gateway error body"


class _FakeClient:
    def __init__(self, response=None, raise_exc: Exception | None = None):
        self._response = response
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def stream(self, method, url, **kwargs):
        if self._raise is not None:
            raise self._raise
        return self._response


def _sse(*chunks: dict) -> list[str]:
    lines = [f"data:{json.dumps(c)}" for c in chunks]
    lines.append("data:[DONE]")
    return lines


def _install_client(client):
    return patch.object(lr.httpx, "AsyncClient", lambda **_: client)


@pytest.mark.asyncio
async def test_gateway_stream_success_yields_delta_and_usage():
    resp = _FakeStreamResponse(200, _sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
    ))
    with patch.object(lr.settings, "LLM_GATEWAY_URL", "http://gw:8799"), \
            _install_client(_FakeClient(response=resp)):
        events = [
            ev async for ev in lr._gateway_stream(
                [{"role": "user", "content": "hi"}], "claude-sonnet-4-5-20250929",
                provider="claude_sub", max_tokens=100, temperature=0.3,
            )
        ]
    assert {"type": "delta", "text": "Hel"} in events
    assert {"type": "delta", "text": "lo"} in events
    assert {"type": "usage", "prompt_tokens": 12, "completion_tokens": 3} in events
    assert all(e["type"] != "_gateway_failed" for e in events)


@pytest.mark.asyncio
async def test_gateway_stream_not_configured_signals_failed():
    with patch.object(lr.settings, "LLM_GATEWAY_URL", ""):
        events = [
            ev async for ev in lr._gateway_stream(
                [{"role": "user", "content": "hi"}], "m",
                provider="claude_sub", max_tokens=10, temperature=0.3,
            )
        ]
    assert events == [{"type": "_gateway_failed", "reason": "not_configured"}]


@pytest.mark.asyncio
async def test_gateway_stream_5xx_signals_failed_before_output():
    resp = _FakeStreamResponse(503, [])
    with patch.object(lr.settings, "LLM_GATEWAY_URL", "http://gw:8799"), \
            _install_client(_FakeClient(response=resp)):
        events = [
            ev async for ev in lr._gateway_stream(
                [{"role": "user", "content": "hi"}], "m",
                provider="claude_sub", max_tokens=10, temperature=0.3,
            )
        ]
    assert len(events) == 1 and events[0]["type"] == "_gateway_failed"


@pytest.mark.asyncio
async def test_gateway_connect_error_signals_failed():
    with patch.object(lr.settings, "LLM_GATEWAY_URL", "http://gw:8799"), \
            _install_client(_FakeClient(raise_exc=ConnectionError("refused"))):
        events = [
            ev async for ev in lr._gateway_stream(
                [{"role": "user", "content": "hi"}], "m",
                provider="codex_sub", max_tokens=10, temperature=0.3,
            )
        ]
    assert events[-1]["type"] == "_gateway_failed"


@pytest.mark.asyncio
async def test_stream_chat_falls_back_to_api_when_gateway_fails():
    """claude_sub gateway failure → falls back to anthropic when
    AI_FALLBACK_TO_API is on."""
    async def fake_anthropic(messages, model, *a, **k):
        yield {"type": "delta", "text": "from-anthropic"}

    with patch.object(lr.settings, "LLM_GATEWAY_URL", ""), \
            patch.object(lr.settings, "AI_FALLBACK_TO_API", True), \
            patch.object(lr, "_resolve_api_key", AsyncMock(return_value="key")), \
            patch.object(lr, "_anthropic_stream", fake_anthropic):
        events = [
            ev async for ev in lr.stream_chat(
                [{"role": "user", "content": "hi"}], provider="claude_sub",
            )
        ]
    assert {"type": "delta", "text": "from-anthropic"} in events
    assert any(e["type"] == "info" and "anthropic" in e["message"] for e in events)


@pytest.mark.asyncio
async def test_stream_chat_no_fallback_when_disabled():
    with patch.object(lr.settings, "LLM_GATEWAY_URL", ""), \
            patch.object(lr.settings, "AI_FALLBACK_TO_API", False), \
            patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")):
        events = [
            ev async for ev in lr.stream_chat(
                [{"role": "user", "content": "hi"}], provider="agy",
            )
        ]
    assert any(e["type"] == "error" and "no API fallback" in e["message"] for e in events)


def test_gateway_providers_registered_and_keyless():
    for prov in ("claude_sub", "codex_sub", "agy"):
        spec = lr.PROVIDERS[prov]
        assert spec.kind == "gateway"
        assert spec.stream_attr == "_gateway_stream"
        assert spec.env_key_attr is None
    assert lr._GATEWAY_FALLBACK == {
        "claude_sub": "anthropic", "codex_sub": "openai", "agy": "gemini",
    }
