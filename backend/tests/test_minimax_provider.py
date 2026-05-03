"""Unit tests for the MiniMax provider + OpenAI-compat tool loop.

Mocks httpx.AsyncClient at the call site so the suite stays offline.
Covers: plain chat, multi-turn tool loop, tool error surfaces, max_turns
guard, missing API key, HTTP non-200.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ai import llm_router as router


# ── fake httpx machinery ──────────────────────────────────────────

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
    def __init__(self, resp: _FakeResp):
        self._resp = resp

    async def __aenter__(self) -> _FakeResp:
        return self._resp

    async def __aexit__(self, *a):
        return None


def _fake_client_factory(responses: list[_FakeResp]):
    queue = list(responses)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def stream(self, method: str, url: str, **kwargs):
            resp = queue.pop(0) if queue else _FakeResp([], status_code=500)
            return _StreamCtx(resp)

    return _Client


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n"


# ── plain chat (no tools) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_minimax_plain_chat_yields_deltas(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {"content": "Hello "}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "world"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory([_FakeResp(lines)]))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="MiniMax-M2.7",
            max_tokens=128,
            temperature=0.3,
            tool_schemas=None,
            tool_dispatch=None,
            max_turns=6,
        )
    ]

    assert events == [
        {"type": "delta", "text": "Hello "},
        {"type": "delta", "text": "world"},
    ]


@pytest.mark.asyncio
async def test_minimax_missing_api_key_yields_error(monkeypatch):
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "")
    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "minimax" in events[0]["message"].lower()


@pytest.mark.asyncio
async def test_minimax_http_error_propagates(monkeypatch):
    monkeypatch.setattr(
        router.httpx, "AsyncClient",
        _fake_client_factory([_FakeResp(["forbidden"], status_code=403)]),
    )
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert events[-1]["type"] == "error"
    assert "403" in events[-1]["message"]


# ── empty-content diagnostics ─────────────────────────────────────


@pytest.mark.asyncio
async def test_minimax_empty_stream_yields_diagnostic_error(monkeypatch):
    """Stream ends with `finish_reason=stop` but never emits any
    `delta.content` or tool_call — caller used to silently see raw="".
    The loop should now yield a diagnostic error event so the
    downstream warning log explains what happened.
    """
    lines = [
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory([_FakeResp(lines)]))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "hi"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    msg = events[0]["message"]
    assert "minimax" in msg.lower()
    assert "no content" in msg.lower()
    assert "finish_reason=stop" in msg


@pytest.mark.asyncio
async def test_minimax_reasoning_only_stream_diagnoses_thinking_burn(monkeypatch):
    """Thinking model (MiniMax-M2 family) emits reasoning_content but
    never produces visible content before hitting max_tokens. The
    diagnostic should call out the reasoning byte count and hint at
    switching to a non-thinking variant.
    """
    lines = [
        _sse({"choices": [{"delta": {"reasoning_content": "Let me think about this carefully. "}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"reasoning_content": "I need to evaluate every headline... "}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "length"}],
              "usage": {"prompt_tokens": 1500, "completion_tokens": 2048}}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory([_FakeResp(lines)]))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "score these headlines"}],
            model="MiniMax-M2.7",
            max_tokens=2048, temperature=0.0,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    types = [e["type"] for e in events]
    assert "error" in types
    err = next(e for e in events if e["type"] == "error")
    msg = err["message"]
    assert "reasoning_content" in msg
    assert "finish_reason=length" in msg
    assert "completion_tokens=2048" in msg
    assert "thinking" in msg.lower()


@pytest.mark.asyncio
async def test_minimax_length_cutoff_with_no_reasoning_hints_max_tokens(monkeypatch):
    """Non-thinking model that runs out of tokens before producing any
    content should at least surface the `(hit max_tokens ...)` hint.
    """
    lines = [
        _sse({"choices": [{"delta": {}, "finish_reason": "length"}],
              "usage": {"prompt_tokens": 100, "completion_tokens": 16}}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory([_FakeResp(lines)]))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-Text-01",
            max_tokens=16, temperature=0.0,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    err = next(e for e in events if e["type"] == "error")
    assert "max_tokens" in err["message"]
    assert "reasoning_content" not in err["message"]


@pytest.mark.asyncio
async def test_minimax_content_filter_finish_reason_is_diagnosed(monkeypatch):
    lines = [
        _sse({"choices": [{"delta": {}, "finish_reason": "content_filter"}]}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory([_FakeResp(lines)]))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.0,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    err = next(e for e in events if e["type"] == "error")
    assert "content_filter" in err["message"]
    assert "safety filter" in err["message"].lower()


@pytest.mark.asyncio
async def test_minimax_reasoning_content_is_not_yielded_as_delta(monkeypatch):
    """Reasoning chain-of-thought must never leak into the visible
    transcript — the JSON parser downstream would choke on prose, and
    the chat UI would expose model internals to the user.
    """
    lines = [
        _sse({"choices": [{"delta": {"reasoning_content": "thinking..."}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "Here is the JSON: "}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "[]"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory([_FakeResp(lines)]))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.0,
            tool_schemas=None, tool_dispatch=None, max_turns=6,
        )
    ]
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    assert deltas == ["Here is the JSON: ", "[]"]
    assert all(e["type"] != "error" for e in events)


# ── tool loop ─────────────────────────────────────────────────────

_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_quote",
            "description": "fetch quote",
            "parameters": {"type": "object"},
        },
    }
]


@pytest.mark.asyncio
async def test_minimax_tool_loop_executes_and_resumes(monkeypatch):
    # Turn 1 — model asks for the tool. Arguments arrive split across two deltas.
    turn1 = [
        _sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "get_quote", "arguments": '{"sy'},
        }]}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": 'mbol":"NVDA","market":"US"}'},
        }]}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]\n",
    ]
    # Turn 2 — model produces final text after seeing the tool result.
    turn2 = [
        _sse({"choices": [{"delta": {"content": "NVDA is at $1234"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n",
    ]

    monkeypatch.setattr(
        router.httpx, "AsyncClient",
        _fake_client_factory([_FakeResp(turn1), _FakeResp(turn2)]),
    )
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    captured: dict = {}

    async def fake_get_quote(args: dict) -> str:
        captured["args"] = args
        return json.dumps({"price": 1234.0, "symbol": args["symbol"]})

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "What is NVDA?"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=_TOOL_SCHEMAS,
            tool_dispatch={"get_quote": fake_get_quote},
            max_turns=6,
        )
    ]

    types = [e["type"] for e in events]
    assert types == ["tool_call", "tool_result", "delta"]
    assert events[0]["name"] == "get_quote"
    assert events[0]["args"] == {"symbol": "NVDA", "market": "US"}
    assert events[1]["is_error"] is False
    assert "1234" in events[1]["summary"]
    assert events[2]["text"] == "NVDA is at $1234"
    assert captured["args"] == {"symbol": "NVDA", "market": "US"}


@pytest.mark.asyncio
async def test_minimax_unknown_tool_returns_error_result(monkeypatch):
    turn1 = [
        _sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_x", "type": "function",
            "function": {"name": "bogus_tool", "arguments": "{}"},
        }]}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]\n",
    ]
    turn2 = [
        _sse({"choices": [{"delta": {"content": "sorry"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(
        router.httpx, "AsyncClient",
        _fake_client_factory([_FakeResp(turn1), _FakeResp(turn2)]),
    )
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=_TOOL_SCHEMAS,
            tool_dispatch={"get_quote": lambda a: None},  # bogus_tool not in dispatch
            max_turns=6,
        )
    ]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True
    assert "unknown tool" in tool_results[0]["summary"].lower()


@pytest.mark.asyncio
async def test_minimax_malformed_tool_args_surfaces_error(monkeypatch):
    turn1 = [
        _sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_x", "type": "function",
            "function": {"name": "get_quote", "arguments": "not-json"},
        }]}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]\n",
    ]
    turn2 = [
        _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]\n",
    ]
    monkeypatch.setattr(
        router.httpx, "AsyncClient",
        _fake_client_factory([_FakeResp(turn1), _FakeResp(turn2)]),
    )
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    handler_called = False

    async def fake_handler(args):
        nonlocal handler_called
        handler_called = True
        return "{}"

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "x"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=_TOOL_SCHEMAS,
            tool_dispatch={"get_quote": fake_handler},
            max_turns=6,
        )
    ]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert tool_results[0]["is_error"] is True
    assert "invalid tool arguments" in tool_results[0]["summary"].lower()
    assert handler_called is False  # never executed because args couldn't be parsed


@pytest.mark.asyncio
async def test_minimax_max_turns_emits_error(monkeypatch):
    # Every turn keeps asking for tools — should bail at max_turns.
    def make_tool_turn() -> list[str]:
        return [
            _sse({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_x", "type": "function",
                "function": {"name": "get_quote", "arguments": '{"symbol":"X","market":"US"}'},
            }]}, "finish_reason": None}]}),
            _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            "data: [DONE]\n",
        ]

    responses = [_FakeResp(make_tool_turn()) for _ in range(3)]
    monkeypatch.setattr(router.httpx, "AsyncClient", _fake_client_factory(responses))
    monkeypatch.setattr(router.settings, "MINIMAX_API_KEY", "test-key")

    async def fake_handler(args):
        return "{}"

    events = [
        ev async for ev in router._minimax_stream(
            messages=[{"role": "user", "content": "loop forever"}],
            model="MiniMax-M2.7",
            max_tokens=128, temperature=0.3,
            tool_schemas=_TOOL_SCHEMAS,
            tool_dispatch={"get_quote": fake_handler},
            max_turns=3,
        )
    ]
    assert events[-1]["type"] == "error"
    assert "max_turns" in events[-1]["message"]


# ── stream_chat dispatch ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_chat_routes_minimax(monkeypatch):
    sentinel = object()

    async def fake_minimax(messages, model, **kwargs):
        yield {"type": "delta", "text": f"routed:{model}"}
        # Make sure the kwargs we expect reach the helper.
        assert kwargs["tool_schemas"] == [sentinel]
        assert kwargs["tool_dispatch"] == {"foo": sentinel}
        assert kwargs["max_turns"] == 4

    monkeypatch.setattr(router, "_minimax_stream", fake_minimax)
    monkeypatch.setattr(router.settings, "MINIMAX_MODEL", "FallbackModel")

    events = [
        ev async for ev in router.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            provider="minimax",
            model=None,
            max_turns=4,
            openai_tool_schemas=[sentinel],
            openai_tool_dispatch={"foo": sentinel},
        )
    ]
    assert events == [{"type": "delta", "text": "routed:FallbackModel"}]


@pytest.mark.asyncio
async def test_stream_chat_minimax_per_request_model_override(monkeypatch):
    captured = {}

    async def fake_minimax(messages, model, **kwargs):
        captured["model"] = model
        yield {"type": "delta", "text": "ok"}

    monkeypatch.setattr(router, "_minimax_stream", fake_minimax)
    with patch.object(router.settings, "MINIMAX_MODEL", "DefaultModel"):
        async for _ in router.stream_chat(
            messages=[{"role": "user", "content": "x"}],
            provider="minimax",
            model="abab6.5s-chat",
        ):
            pass
    assert captured["model"] == "abab6.5s-chat"
