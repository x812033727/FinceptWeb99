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
async def test_round_refunds_unconsumed_quota_on_round_crash(
    client: AsyncClient,
):
    """If the round generator raises before any persona produces a turn,
    every reserved persona-credit must be refunded.

    A persona's own stream raising is NOT this scenario — `run_round`
    catches per-persona exceptions and still emits a placeholder
    `turn_end`, so those reserved credits are considered "consumed".
    The refund path fires when the body itself crashes (e.g.
    `gather_market_context` blew up before the persona loop even
    started) — then no `turn_end` events arrive and the finally
    block returns the full cost."""
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

    async def _broken_context(*_a, **_kw):
        raise RuntimeError("market data unavailable")

    with patch(
        "services.discussion_service.gather_market_context",
        side_effect=_broken_context,
    ), patch(
        "api.discussion.router._refund", new_callable=AsyncMock,
    ) as refund:
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/round", headers=h,
        )

    # SSE response itself is 200 — the error is delivered as a `data:
    # {"type":"error",...}` frame inside the stream, then [DONE].
    assert r.status_code == 200
    # All 5 reserved credits refunded because no turn_end fired.
    assert refund.await_count == 1
    assert refund.await_args.kwargs.get("count") == 5


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

    # The router's refund-then-raise pattern means the exception bubbles
    # past FastAPI's default exception handler in TestClient mode (the
    # AsyncClient against ASGI re-raises server exceptions unless
    # configured otherwise). Wrap in pytest.raises so the assertion
    # below — that refund DID run before the exception bubbled — still
    # gets evaluated.
    with patch(
        "services.discussion_service.synthesize_conclusion", side_effect=_boom,
    ), patch(
        "api.discussion.router._refund", new_callable=AsyncMock,
    ) as refund:
        with pytest.raises(RuntimeError, match="synthesizer crashed"):
            await client.post(
                f"/api/discussion/sessions/{discussion_id}/conclude", headers=h,
            )

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


# ── round-context snapshots (PR #135) ─────────────────────────────


@pytest.mark.asyncio
async def test_get_round_contexts_returns_persisted_snapshots(
    client: AsyncClient, db_session: AsyncSession,
):
    """End-to-end: create a discussion, seed a snapshot row directly,
    GET /sessions/{id}/contexts returns it owner-scoped."""
    from models.discussion_round_context import DiscussionRoundContext

    h = await _register(client, "disc_ctx@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={
            "topic": "x", "rules": "y",
            "persona_ids": ["buffett", "lynch"],
        },
    )
    discussion_id = r.json()["id"]

    db_session.add(DiscussionRoundContext(
        discussion_id=uuid.UUID(discussion_id),
        round=1,
        context={"market": "TW", "top_gainers": [{"symbol": "2330"}]},
    ))
    await db_session.commit()

    r = await client.get(
        f"/api/discussion/sessions/{discussion_id}/contexts",
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["round"] == 1
    assert body[0]["context"]["top_gainers"][0]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_get_round_contexts_owner_scoped_returns_404_for_others(
    client: AsyncClient,
):
    h_a = await _register(client, "disc_ctx_a@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h_a,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = r.json()["id"]

    h_b = await _register(client, "disc_ctx_b@example.com")
    r = await client.get(
        f"/api/discussion/sessions/{discussion_id}/contexts",
        headers=h_b,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_round_contexts_empty_for_legacy_discussion(
    client: AsyncClient,
):
    """A discussion that has no snapshots (e.g. created before PR
    #135 wired the writes, or all rounds' snapshot writes failed)
    returns [] rather than 404 — the absence of snapshots isn't an
    error condition."""
    h = await _register(client, "disc_ctx_empty@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = r.json()["id"]

    r = await client.get(
        f"/api/discussion/sessions/{discussion_id}/contexts",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json() == []


# ── scoreboard endpoint (PR #140) ────────────────────────────────


@pytest.mark.asyncio
async def test_get_scoreboard_returns_400_when_no_conclusion(
    client: AsyncClient,
):
    """Newly-created discussion has no conclusion yet — endpoint
    bails with 400 rather than returning an empty rows list."""
    h = await _register(client, "scoreboard_no_conc@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = r.json()["id"]

    r = await client.get(
        f"/api/discussion/sessions/{discussion_id}/scoreboard",
        headers=h,
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_scoreboard_owner_scoped_404_for_others(
    client: AsyncClient,
):
    h_a = await _register(client, "scoreboard_a@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h_a,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = r.json()["id"]

    h_b = await _register(client, "scoreboard_b@example.com")
    r = await client.get(
        f"/api/discussion/sessions/{discussion_id}/scoreboard",
        headers=h_b,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_scoreboard_returns_rows_when_concluded(
    client: AsyncClient, db_session: AsyncSession, monkeypatch,
):
    """Discussion with a conclusion → endpoint returns rows even
    when the cron hasn't persisted `daily_close_prices` yet (the
    on-demand compute path)."""
    # Block the scoreboard service's live-fallback path
    # (`tw_market_service.get_history`) so this test can't reach a
    # real TWSE/FinMind upstream in CI. Without this, the on-demand
    # compute finds zero archived bars, falls through to live, gets
    # today's bar back, and `days_resolved` ends up 1 instead of 0.
    from services import tw_market_service as _tw_svc

    async def _no_history(symbol, months=12):
        return []

    monkeypatch.setattr(_tw_svc, "get_history", _no_history)

    h = await _register(client, "scoreboard_ondemand@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = r.json()["id"]

    # Manually attach a conclusion so the endpoint considers it
    # eligible — we don't need to actually run a round.
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.conclusion = {
        "recommended_symbols": ["2330"],
        "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.5,
    }
    await db_session.commit()

    r = await client.get(
        f"/api/discussion/sessions/{discussion_id}/scoreboard",
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["discussion_id"] == discussion_id
    assert isinstance(body["rows"], list)
    assert len(body["rows"]) == 1
    assert body["rows"][0]["symbol"] == "2330"
    # No OHLCV pre-seeded → days_resolved=0 with all-None arrays.
    assert body["rows"][0]["days_resolved"] == 0


# ── inject_user_message endpoint ──────────────────────────────────


@pytest.mark.asyncio
async def test_inject_201_when_round_done_and_owner(
    client: AsyncClient, db_session: AsyncSession,
):
    h = await _register(client, "disc_inject_ok@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "t", "rules": "r", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    # Force current_round=1 (no turns required — only the round
    # counter check matters at the API layer).
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.current_round = 1
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/inject",
        headers=h,
        json={"content": "請聚焦在 2330"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["persona_id"] == "_user"
    assert body["stance"] == "user_input"
    assert body["round"] == 1
    assert "2330" in body["content"]


@pytest.mark.asyncio
async def test_inject_400_before_first_round(
    client: AsyncClient,
):
    h = await _register(client, "disc_inject_no_round@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "t", "rules": "r", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]
    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/inject",
        headers=h,
        json={"content": "請聚焦在 2330"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_inject_404_for_non_owner(
    client: AsyncClient, db_session: AsyncSession,
):
    h_a = await _register(client, "disc_inject_a@example.com")
    h_b = await _register(client, "disc_inject_b@example.com")
    create = await client.post(
        "/api/discussion/sessions",
        headers=h_a,
        json={"topic": "t", "rules": "r", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = create.json()["id"]

    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.current_round = 1
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/inject",
        headers=h_b,
        json={"content": "x"},
    )
    assert r.status_code == 404


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
