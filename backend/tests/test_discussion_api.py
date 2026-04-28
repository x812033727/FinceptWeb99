"""HTTP-surface integration tests for the discussion API.

Service-layer logic (CRUD, run_round generator, synthesizer parsing,
batch persona resolution, status reset on exception) is exhaustively
covered in `test_discussion_service.py` as pure unit tests. Only the
router-level behaviour lives here:

  - request schema → 201 / 422 / 400
  - owner-scoped reads (cross-user 404)
  - SSE event frame shape and `[DONE]` terminator
  - quota refund on partial-round exit
  - conclude endpoint refund-on-failure
  - 409 when a round is already in progress

`stream_chat` and `gather_market_context` are patched out — these tests
assert HTTP plumbing, not LLM behaviour.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.user import User, UserRole


# ── helpers ────────────────────────────────────────────────────────


async def _register(client: AsyncClient, email: str) -> dict:
    await client.post("/api/auth/register", json={
        "email": email, "password": "ValidPass99!",
    })
    r = await client.post("/api/auth/login", json={
        "email": email, "password": "ValidPass99!",
    })
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _stream_events_sequence(payloads: list[str]):
    """Mimic stream_chat: yield `delta` events, one persona reply each call."""
    counter = {"i": 0}

    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        idx = min(counter["i"], len(payloads) - 1)
        counter["i"] += 1
        yield {"type": "delta", "text": payloads[idx]}
    return _gen


# ── create / read / delete (owner-scoped) ──────────────────────────


@pytest.mark.asyncio
async def test_create_discussion_201(client: AsyncClient):
    h = await _register(client, "disc_create@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={
            "topic": "Test topic",
            "rules": "Test rules",
            "persona_ids": ["buffett", "lynch"],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft"
    assert body["current_round"] == 0
    assert body["persona_ids"] == ["buffett", "lynch"]


@pytest.mark.asyncio
async def test_create_discussion_rejects_too_few_personas(client: AsyncClient):
    """Pydantic schema enforces min_length=2 → 422 before service layer."""
    h = await _register(client, "disc_one_persona@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett"]},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_get_discussion_owner_scoped_returns_404_for_others(
    client: AsyncClient,
):
    """Two users; A creates a discussion, B can't read it."""
    h_a = await _register(client, "disc_owner_a@example.com")
    h_b = await _register(client, "disc_owner_b@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h_a,
        json={
            "topic": "owner-scoped",
            "rules": "rules",
            "persona_ids": ["buffett", "lynch"],
        },
    )
    discussion_id = create.json()["id"]

    # A can read.
    r_a = await client.get(f"/api/discussion/sessions/{discussion_id}", headers=h_a)
    assert r_a.status_code == 200

    # B cannot.
    r_b = await client.get(f"/api/discussion/sessions/{discussion_id}", headers=h_b)
    assert r_b.status_code == 404


@pytest.mark.asyncio
async def test_delete_discussion_owner_scoped(client: AsyncClient):
    h_a = await _register(client, "disc_del_a@example.com")
    h_b = await _register(client, "disc_del_b@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h_a,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    # B can't delete.
    r_b = await client.delete(f"/api/discussion/sessions/{discussion_id}", headers=h_b)
    assert r_b.status_code == 404

    # A can.
    r_a = await client.delete(f"/api/discussion/sessions/{discussion_id}", headers=h_a)
    assert r_a.status_code == 204


# ── round endpoint ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_round_emits_sse_frames_and_terminator(
    client: AsyncClient, db_session: AsyncSession,
):
    """Happy path: 2-persona round emits round_start + context + per-turn
    frames + round_end + [DONE] terminator."""
    h = await _register(client, "disc_round_happy@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    replies = [
        '{"stance": "supplement", "content": "first"}',
        '{"stance": "agree", "content": "second"}',
    ]
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events_sequence(replies),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/round", headers=h,
        )

    assert r.status_code == 200
    body = r.text
    # SSE terminator must be present
    assert "data: [DONE]\n\n" in body
    # Each event is its own `data: {...}\n\n` frame
    types = [json.loads(line[6:])["type"]
             for line in body.splitlines()
             if line.startswith("data: ") and not line.endswith("[DONE]")]
    assert types[0] == "round_start"
    assert "context" in types
    assert types.count("turn_end") == 2
    assert types[-1] == "round_end"


@pytest.mark.asyncio
async def test_round_returns_409_when_already_running(
    client: AsyncClient, db_session: AsyncSession,
):
    """Manually flip status to RUNNING and verify the endpoint refuses
    a concurrent round (the in-progress guard)."""
    h = await _register(client, "disc_round_conflict@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    # Mutate via the test DB session.
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.status = "running"
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/round", headers=h,
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_round_refunds_unconsumed_quota_on_persona_failure(
    client: AsyncClient,
):
    """5-persona round, but the 2nd persona's stream raises. The 3 personas
    that never produced turn_end should be refunded.

    Full charge = 5; turn_end count ≈ 2 (first persona OK, second yields
    error event but still ends → counted; third aborts because of TimeoutError
    before turn_end). We assert the refund helper was called with > 0 count.
    """
    h = await _register(client, "disc_refund@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={
            "topic": "x", "rules": "y",
            "persona_ids": ["buffett", "lynch", "munger", "graham", "fisher"],
        },
    )
    discussion_id = create.json()["id"]

    call_n = {"i": 0}

    async def _flaky_stream(*_a, **_kw):
        call_n["i"] += 1
        if call_n["i"] == 1:
            yield {"type": "delta", "text": '{"stance":"agree","content":"ok"}'}
        else:
            # Subsequent personas all fail with TimeoutError so run_round's
            # timeout-handler emits an error event then a placeholder turn_end.
            # Crucially we abort BEFORE turn_end gets emitted by raising —
            # the router's refund counter only sees 1 completed.
            raise RuntimeError("provider crash")

    with patch(
        "services.discussion_service.stream_chat", side_effect=_flaky_stream,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ), patch(
        "api.discussion.router._refund", new_callable=AsyncMock,
    ) as refund:
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/round", headers=h,
        )

    assert r.status_code == 200
    # Refund was called and the count is > 0 (some personas didn't complete).
    assert refund.await_count >= 1
    refund_count = refund.await_args.kwargs.get("count")
    assert refund_count is not None
    assert refund_count > 0


# ── conclude endpoint ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conclude_rejects_zero_round_discussion(client: AsyncClient):
    """Calling /conclude before any round has run must 400, not silently
    burn a quota credit."""
    h = await _register(client, "disc_conclude_premature@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/conclude", headers=h,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_conclude_refunds_quota_when_synthesize_raises(
    client: AsyncClient, db_session: AsyncSession,
):
    """If the synthesizer LLM call throws, the credit reserved up-front
    must be refunded (otherwise users lose 1 credit per failed conclude)."""
    h = await _register(client, "disc_conclude_refund@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    # Flip current_round to 1 so the conclude endpoint accepts the call;
    # we don't actually need turns persisted because we're testing the
    # refund path on synthesize failure.
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.current_round = 1
    await db_session.commit()

    async def _boom(*_a, **_kw):
        raise RuntimeError("synthesizer crashed")

    with patch(
        "services.discussion_service.synthesize_conclusion", side_effect=_boom,
    ), patch(
        "api.discussion.router._refund", new_callable=AsyncMock,
    ) as refund:
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/conclude", headers=h,
        )

    # Endpoint propagates the 500 from the synthesizer; refund still ran.
    assert r.status_code in (500, 502, 503)
    assert refund.await_count == 1
    assert refund.await_args.kwargs.get("count") == 1


# ── Edit (PATCH) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_rejected_after_round(
    client: AsyncClient, db_session: AsyncSession,
):
    """PATCH must 400 once the discussion has advanced past draft —
    persona roster + rules are frozen so prior turns stay coherent."""
    h = await _register(client, "disc_patch_locked@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.status = "done"
    await db_session.commit()

    r = await client.patch(
        f"/api/discussion/sessions/{discussion_id}",
        headers=h,
        json={"topic": "new topic"},
    )
    assert r.status_code == 400


# ── User fixture for service-layer needs ───────────────────────────


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """For tests that need to inject a known user without going through
    the registration HTTP path."""
    user = User(
        email=f"disc-api-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user
