"""
Unit tests for the AI quota refund mechanism.

Asserts the daily AI quota counter is decremented when:
- Agent ID validation fails after the increment
- Claude Agent provider is requested but disabled
- Claude Agent is requested by a non-analyst role
- The LLM stream raises before yielding any content
- The LLM stream completes without producing any delta/tool event

And verifies normal completion does NOT trigger a refund.
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole


# ── helpers ────────────────────────────────────────────────────────

async def _register_login(client: AsyncClient, email: str) -> str:
    await client.post("/api/auth/register", json={"email": email, "password": "Pass99!!"})
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return r.json()["access_token"]


async def _promote(db: AsyncSession, email: str, role: UserRole, client: AsyncClient) -> str:
    result = await db.execute(select(User).where(User.email == email))
    u = result.scalar_one()
    u.role = role
    await db.commit()
    r = await client.post("/api/auth/login", json={"email": email, "password": "Pass99!!"})
    return r.json()["access_token"]


# ── tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refund_when_get_agent_raises(client: AsyncClient):
    """Pydantic validates `agent_id` against the agent registry, so a bogus
    string is a 422. We instead mock the resolver to raise to exercise the
    400 branch which refunds the quota."""
    tok = await _register_login(client, "refund_get_agent_raises@test.com")
    with patch("api.ai_agents.router.get_agent_resolved",
               new=AsyncMock(side_effect=ValueError("agent not found"))), \
         patch("api.ai_agents.router._refund_quota", new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/chat",
            json={"agent_id": "market_analyst",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 400
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_refund_when_claude_agent_disabled(client: AsyncClient, db_session: AsyncSession):
    email = "refund_ca_disabled@test.com"
    await _register_login(client, email)
    tok = await _promote(db_session, email, UserRole.analyst, client)

    with patch("api.ai_agents.router._refund_quota", new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/chat",
            json={"agent_id": "claude_research",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 503
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_refund_when_claude_agent_role_insufficient(client: AsyncClient):
    tok = await _register_login(client, "refund_ca_viewer@test.com")
    with patch("api.ai_agents.router.settings.CLAUDE_AGENT_ENABLED", True), \
         patch("api.ai_agents.router._refund_quota", new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/chat",
            json={"agent_id": "claude_research",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 403
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_refund_when_stream_yields_nothing(client: AsyncClient):
    tok = await _register_login(client, "refund_empty_stream@test.com")

    async def empty_stream(*_a, **_kw):
        if False:
            yield  # pragma: no cover — async generator that never yields

    with patch("api.ai_agents.router.stream_chat", side_effect=empty_stream), \
         patch("api.ai_agents.router._refund_quota", new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/chat",
            json={"agent_id": "market_analyst",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200
    # Force the generator to fully drain
    _ = r.text
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_refund_when_stream_raises(client: AsyncClient):
    tok = await _register_login(client, "refund_stream_raise@test.com")

    async def boom_stream(*_a, **_kw):
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover

    with patch("api.ai_agents.router.stream_chat", side_effect=boom_stream), \
         patch("api.ai_agents.router._refund_quota", new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/chat",
            json={"agent_id": "market_analyst",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200
    body = r.text
    assert "provider exploded" in body
    refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_refund_on_normal_completion(client: AsyncClient):
    tok = await _register_login(client, "no_refund_ok@test.com")

    async def good_stream(*_a, **_kw):
        yield {"type": "delta", "text": "hello"}
        yield {"type": "delta", "text": " world"}

    with patch("api.ai_agents.router.stream_chat", side_effect=good_stream), \
         patch("api.ai_agents.router._refund_quota", new_callable=AsyncMock) as refund:
        r = await client.post(
            "/api/ai/chat",
            json={"agent_id": "market_analyst",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {tok}"},
        )
    assert r.status_code == 200
    _ = r.text
    refund.assert_not_awaited()
