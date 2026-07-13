"""LLM gateway provider (claude_sub / codex_sub / agy) — SSE parsing,
success streaming, and the fail-before-output → API fallback path.

The gateway HTTP layer is mocked; no host sidecar is required.
"""
from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
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
                [{"role": "user", "content": "hi"}], "claude-sonnet-5",
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


# ── auto-upgrade to subscription (AI_AUTO_UPGRADE_TO_SUB) ──────────

def _recording_gateway(events: list[dict], calls: list[dict]):
    async def fake_gateway(messages, model, *, provider, **kwargs):
        calls.append({"model": model, "provider": provider})
        for ev in events:
            yield ev
    return fake_gateway


def _recording_anthropic(calls: list[dict]):
    async def fake_anthropic(messages, model, *a, **k):
        calls.append({"model": model})
        yield {"type": "delta", "text": "from-anthropic"}
    return fake_anthropic


@contextmanager
def _upgrade_env(*extra, url="http://gw:8799", token="tok", flag=True):
    """Gateway-configured settings plus any extra patches, one context."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(lr.settings, "LLM_GATEWAY_URL", url))
        stack.enter_context(patch.object(lr.settings, "LLM_GATEWAY_TOKEN", token))
        stack.enter_context(patch.object(lr.settings, "AI_AUTO_UPGRADE_TO_SUB", flag))
        for p in extra:
            stack.enter_context(p)
        yield


@pytest.mark.asyncio
async def test_auto_upgrade_anthropic_when_no_key_and_gateway_configured():
    gw_calls: list[dict] = []
    fake_gw = _recording_gateway([{"type": "delta", "text": "from-sub"}], gw_calls)
    with _upgrade_env(
        patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")),
        patch.object(lr, "_gateway_stream", fake_gw),
    ):
        events = [
            ev async for ev in lr.stream_chat(
                [{"role": "user", "content": "hi"}], provider="anthropic",
            )
        ]
    assert gw_calls == [{"model": "claude-sonnet-5", "provider": "claude_sub"}]
    assert events[0] == {
        "type": "provider", "provider": "claude_sub", "model": "claude-sonnet-5",
    }
    assert {"type": "delta", "text": "from-sub"} in events


@pytest.mark.asyncio
async def test_auto_upgrade_keeps_explicit_model():
    gw_calls: list[dict] = []
    fake_gw = _recording_gateway([{"type": "delta", "text": "ok"}], gw_calls)
    with _upgrade_env(
        patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")),
        patch.object(lr, "_gateway_stream", fake_gw),
    ):
        [ev async for ev in lr.stream_chat(
            [{"role": "user", "content": "hi"}], provider="anthropic",
            model="claude-haiku-4-5-20251001",
        )]
    assert gw_calls == [{"model": "claude-haiku-4-5-20251001", "provider": "claude_sub"}]


@pytest.mark.asyncio
async def test_auto_upgrade_claude_agent_when_env_key_empty():
    gw_calls: list[dict] = []
    agent_calls: list[dict] = []
    fake_gw = _recording_gateway([{"type": "delta", "text": "ok"}], gw_calls)

    async def fake_agent(messages, model, **kwargs):
        agent_calls.append({"model": model})
        yield {"type": "delta", "text": "from-agent"}

    with _upgrade_env(
        patch.object(lr.settings, "ANTHROPIC_API_KEY", ""),
        patch.object(lr, "_gateway_stream", fake_gw),
        patch.object(lr, "_claude_agent_stream", fake_agent),
    ):
        [ev async for ev in lr.stream_chat(
            [{"role": "user", "content": "hi"}], provider="claude_agent",
            mcp_server=object(),
        )]
    assert gw_calls and gw_calls[0]["provider"] == "claude_sub"
    assert agent_calls == []


@pytest.mark.asyncio
async def test_no_upgrade_when_key_present():
    gw_calls: list[dict] = []
    anth_calls: list[dict] = []
    with _upgrade_env(
        patch.object(lr, "_resolve_api_key", AsyncMock(return_value="sk-key")),
        patch.object(lr, "_gateway_stream", _recording_gateway([], gw_calls)),
        patch.object(lr, "_anthropic_stream", _recording_anthropic(anth_calls)),
    ):
        events = [ev async for ev in lr.stream_chat(
            [{"role": "user", "content": "hi"}], provider="anthropic",
        )]
    assert gw_calls == []
    assert anth_calls == [{"model": "claude-haiku-4-5-20251001"}]
    assert all(e["type"] != "provider" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,token,flag", [
    ("", "tok", True),        # gateway URL unset
    ("http://gw:8799", "", True),   # token unset
    ("http://gw:8799", "tok", False),  # feature flag off
])
async def test_no_upgrade_when_gateway_unset_or_flag_disabled(url, token, flag):
    gw_calls: list[dict] = []
    anth_calls: list[dict] = []
    with _upgrade_env(
        patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")),
        patch.object(lr, "_gateway_stream", _recording_gateway([], gw_calls)),
        patch.object(lr, "_anthropic_stream", _recording_anthropic(anth_calls)),
        url=url, token=token, flag=flag,
    ):
        [ev async for ev in lr.stream_chat(
            [{"role": "user", "content": "hi"}], provider="anthropic",
        )]
    assert gw_calls == []
    assert len(anth_calls) == 1


@pytest.mark.asyncio
async def test_upgraded_call_gateway_failure_falls_back_without_loop():
    """The anti-recursion guard: upgraded anthropic → dead gateway →
    fallback to the real anthropic path exactly once, no re-upgrade."""
    gw_calls: list[dict] = []
    anth_calls: list[dict] = []
    fake_gw = _recording_gateway(
        [{"type": "_gateway_failed", "reason": "http 503"}], gw_calls,
    )
    with _upgrade_env(
        patch.object(lr.settings, "AI_FALLBACK_TO_API", True),
        patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")),
        patch.object(lr, "_gateway_stream", fake_gw),
        patch.object(lr, "_anthropic_stream", _recording_anthropic(anth_calls)),
    ):
        events = [ev async for ev in lr.stream_chat(
            [{"role": "user", "content": "hi"}], provider="anthropic",
        )]
    assert len(gw_calls) == 1
    assert len(anth_calls) == 1
    assert any(e["type"] == "info" and "anthropic" in e["message"] for e in events)
    assert {"type": "delta", "text": "from-anthropic"} in events


@pytest.mark.asyncio
async def test_explicit_claude_sub_unchanged():
    gw_calls: list[dict] = []
    fake_gw = _recording_gateway([{"type": "delta", "text": "ok"}], gw_calls)
    with _upgrade_env(
        patch.object(lr, "_resolve_api_key", AsyncMock(return_value="")),
        patch.object(lr, "_gateway_stream", fake_gw),
    ):
        events = [ev async for ev in lr.stream_chat(
            [{"role": "user", "content": "hi"}], provider="claude_sub",
        )]
    assert gw_calls == [{"model": "claude-sonnet-5", "provider": "claude_sub"}]
    assert all(e["type"] != "provider" for e in events)


def test_gateway_providers_registered_and_keyless():
    for prov in ("claude_sub", "codex_sub", "agy"):
        spec = lr.PROVIDERS[prov]
        assert spec.kind == "gateway"
        assert spec.stream_attr == "_gateway_stream"
        assert spec.env_key_attr is None
    assert lr._GATEWAY_FALLBACK == {
        "claude_sub": "anthropic", "codex_sub": "openai", "agy": "gemini",
    }
