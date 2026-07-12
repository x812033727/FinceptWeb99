"""Local tests for the gateway app (run on the host, not in CI).

    cd gateway && python -m pytest test_app.py

Providers are monkeypatched so no CLI/SDK/credentials are needed.
"""
import json

import pytest
from fastapi.testclient import TestClient

import app as gw
from providers import ProviderError


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.setattr(gw, "_TOKEN", "")


def _parse_sse(text: str):
    events = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


def test_health():
    c = TestClient(gw.app)
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_stream_success(monkeypatch):
    async def fake(messages, model):
        yield "Hel"
        yield "lo"
    monkeypatch.setitem(gw._PROVIDERS, "claude_sub", fake)
    c = TestClient(gw.app)
    r = c.post(
        "/v1/chat/completions",
        headers={"X-LLM-Provider": "claude_sub"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    texts = [e["choices"][0]["delta"].get("content") for e in events if e.get("choices")]
    assert "Hel" in texts and "lo" in texts
    # final usage chunk present
    assert any(e.get("usage") for e in events)


def test_preflight_error_returns_502(monkeypatch):
    async def boom(messages, model):
        raise ProviderError("subscription exhausted")
        yield  # pragma: no cover
    monkeypatch.setitem(gw._PROVIDERS, "codex_sub", boom)
    c = TestClient(gw.app)
    r = c.post(
        "/v1/chat/completions",
        headers={"X-LLM-Provider": "codex_sub"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 502
    assert "exhausted" in r.json()["error"]["message"]


def test_unknown_provider_400():
    c = TestClient(gw.app)
    r = c.post(
        "/v1/chat/completions",
        headers={"X-LLM-Provider": "nope"},
        json={"messages": []},
    )
    assert r.status_code == 400


def test_auth_enforced(monkeypatch):
    monkeypatch.setattr(gw, "_TOKEN", "secret")
    c = TestClient(gw.app)
    r = c.post(
        "/v1/chat/completions",
        headers={"X-LLM-Provider": "claude_sub"},
        json={"messages": []},
    )
    assert r.status_code == 401
