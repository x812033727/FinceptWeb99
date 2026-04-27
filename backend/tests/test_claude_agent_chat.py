"""
Integration tests for the claude_agent SSE path.

Real Anthropic calls aren't made — we patch llm_router.stream_chat to
yield a deterministic event sequence (delta → tool_call → tool_result
→ delta → done) and assert the SSE frames arrive in the right order.
"""
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import patch

from models.user import User, UserRole


# ── helpers ────────────────────────────────────────────────────────

async def _register_login(client: AsyncClient, email: str, password: str = "Pass99!!") -> str:
    await client.post("/api/auth/register", json={"email": email, "password": password})
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.json()["access_token"]


async def _promote(db: AsyncSession, email: str, role: UserRole, client: AsyncClient, password: str = "Pass99!!") -> str:
    result = await db.execute(select(User).where(User.email == email))
    u = result.scalar_one()
    u.role = role
    await db.commit()
    r = await client.post("/api/auth/login", json={"email": email, "password": password})
    return r.json()["access_token"]


def _fake_stream_sequence():
    """Build a fake async generator factory that yields a scripted SDK event stream."""
    async def _gen(*args, **kwargs):
        yield {"type": "delta", "text": "Checking the quote… "}
        yield {"type": "tool_call", "id": "tu_01", "name": "get_quote", "args": {"symbol": "AAPL", "market": "US"}}
        yield {"type": "tool_result", "id": "tu_01", "name": "get_quote",
               "summary": '{"symbol":"AAPL","price":180.0}', "is_error": False}
        yield {"type": "delta", "text": "The price is $180."}
    return _gen


async def _collect_sse(resp) -> list[dict]:
    """Read full SSE body, return list of parsed JSON events (and raw [DONE])."""
    import json
    text = resp.text
    events: list = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            events.append({"__done__": True})
        else:
            events.append(json.loads(payload))
    return events


# ── tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claude_agent_disabled_returns_503(client: AsyncClient, db_session: AsyncSession):
    from config import settings
    email = "ca_disabled@test.com"
    await _register_login(client, email)
    tok = await _promote(db_session, email, UserRole.analyst, client)

    # Default flipped to True in PR #55 (auto-detect SDK); explicitly
    # disable here to exercise the off-by-flag 503 path.
    with patch.object(settings, "CLAUDE_AGENT_ENABLED", False):
        r = await client.post("/api/ai/chat",
            json={"agent_id": "claude_research",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 503
    assert "disabled" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_claude_agent_viewer_gets_403(client: AsyncClient, db_session: AsyncSession):
    from config import settings
    email = "ca_viewer@test.com"
    tok = await _register_login(client, email)  # defaults to viewer

    with patch.object(settings, "CLAUDE_AGENT_ENABLED", True):
        r = await client.post("/api/ai/chat",
            json={"agent_id": "claude_research",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 403
    assert "analyst" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_claude_agent_analyst_streams_tool_events(
    client: AsyncClient, db_session: AsyncSession,
):
    from config import settings
    email = "ca_analyst@test.com"
    await _register_login(client, email)
    tok = await _promote(db_session, email, UserRole.analyst, client)

    with patch.object(settings, "CLAUDE_AGENT_ENABLED", True), \
         patch("api.ai_agents.router.stream_chat", _fake_stream_sequence()):
        r = await client.post("/api/ai/chat",
            json={"agent_id": "claude_research",
                  "messages": [{"role": "user", "content": "Quote AAPL"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r.status_code == 200
    events = await _collect_sse(r)
    # Expect: delta, tool_call, tool_result, delta, [DONE]
    assert len(events) >= 5
    assert events[0] == {"delta": "Checking the quote… "}
    assert events[1]["tool_call"]["name"] == "get_quote"
    assert events[1]["tool_call"]["id"] == "tu_01"
    assert events[2]["tool_result"]["id"] == "tu_01"
    assert events[2]["tool_result"]["is_error"] is False
    assert events[3] == {"delta": "The price is $180."}
    assert events[-1] == {"__done__": True}


@pytest.mark.asyncio
async def test_claude_agent_provider_override_on_existing_persona(
    client: AsyncClient, db_session: AsyncSession,
):
    """Analyst can run any existing persona through claude_agent via provider override."""
    from config import settings
    email = "ca_override@test.com"
    await _register_login(client, email)
    tok = await _promote(db_session, email, UserRole.analyst, client)

    captured_kwargs: dict = {}

    def _capturing_stream():
        async def _gen(*args, **kwargs):
            captured_kwargs.update(kwargs)
            yield {"type": "delta", "text": "ok"}
        return _gen

    with patch.object(settings, "CLAUDE_AGENT_ENABLED", True), \
         patch("api.ai_agents.router.stream_chat", _capturing_stream()):
        r = await client.post("/api/ai/chat",
            json={"agent_id": "market_analyst",  # persona defaults to openai
                  "messages": [{"role": "user", "content": "analyse AAPL"}],
                  "provider": "claude_agent"},  # override
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r.status_code == 200
    assert captured_kwargs["provider"] == "claude_agent"
    assert captured_kwargs["mcp_server"] is not None   # toolset built
    assert len(captured_kwargs["allowed_tools"]) > 0
