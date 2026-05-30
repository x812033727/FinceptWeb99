"""Admin AI-quota exemption.

Admins are the operator role; the daily AI request quota exists to cap
viewer / analyst spend, not the operator. Before the exemption, a manual
multi-round discussion run (cost = len(persona_ids) per round) could 429
part-way through — e.g. 6 personas x 4 rounds = 24 > the analyst cap — and
the frontend loop stops cleanly on the first 429, surfacing as "ran 3 of 5
rounds then stopped".

These tests assert:
  - an admin's /round survives a counter pre-loaded far above the cap
    (would 429 for a non-exempt role).
  - /auth/me reports ai_requests_remaining=null for admins (frontend
    treats null as uncapped and skips the multi-round pre-flight warning)
    while still reporting a concrete integer for a viewer.
"""
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import cache_incr, key_ai_counter
from models.user import User, UserRole


# ── helpers ────────────────────────────────────────────────────────


async def _register(client: AsyncClient, email: str) -> dict:
    await client.post(
        "/api/auth/register", json={"email": email, "password": "ValidPass99!"},
    )
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "ValidPass99!"},
    )
    return {"token": r.json()["access_token"]}


async def _user_id(db: AsyncSession, email: str) -> str:
    u = await db.scalar(select(User).where(User.email == email))
    return str(u.id)


async def _promote(db: AsyncSession, email: str, role: UserRole) -> None:
    u = await db.scalar(select(User).where(User.email == email))
    u.role = role
    await db.commit()


async def _login(client: AsyncClient, email: str) -> str:
    r = await client.post(
        "/api/auth/login", json={"email": email, "password": "ValidPass99!"},
    )
    return r.json()["access_token"]


def _stream_one_reply():
    """Mimic stream_chat: each call yields one persona's JSON reply."""

    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": '{"stance": "agree", "content": "ok"}'}

    return _gen


# ── tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_round_not_rate_limited(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "disc_admin_quota@example.com"
    await _register(client, email)
    uid = await _user_id(db_session, email)
    await _promote(db_session, email, UserRole.admin)
    tok = await _login(client, email)

    # Blow the counter far past any conceivable cap so a non-exempt path
    # would 429 on the very next /round.
    for _ in range(500):
        await cache_incr(key_ai_counter(uid), ttl_seconds=86400)

    create = await client.post(
        "/api/discussion/sessions",
        headers={"Authorization": f"Bearer {tok}"},
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_one_reply(),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/round",
            headers={"Authorization": f"Bearer {tok}"},
        )

    # Admin is exempt: the round streams (200) rather than 429.
    assert r.status_code == 200, r.text
    assert "data: [DONE]\n\n" in r.text


@pytest.mark.asyncio
async def test_me_reports_unlimited_for_admin(
    client: AsyncClient, db_session: AsyncSession,
):
    email = "disc_admin_me@example.com"
    reg = await _register(client, email)

    # Viewer: a concrete integer remaining.
    me_viewer = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {reg['token']}"},
    )
    assert isinstance(me_viewer.json()["ai_requests_remaining"], int)

    await _promote(db_session, email, UserRole.admin)
    tok = await _login(client, email)
    me_admin = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {tok}"},
    )
    # Admin: null ("unlimited") so the frontend skips the pre-flight warning.
    assert me_admin.json()["ai_requests_remaining"] is None
