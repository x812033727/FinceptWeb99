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
async def test_create_discussion_surfaces_unexpected_failure_detail(
    client: AsyncClient,
):
    """Pre-PR: any exception other than ValueError surfaced as a bare
    `Internal Server Error` 500 with no diagnostic content. Now the
    handler catches the broad case, logs it, and returns the actual
    exception class + message in the `detail` field so the user's
    error banner is informative.

    Simulate the failure by patching the service layer to raise a
    RuntimeError mid-create (mimics a DB error / schema drift / FK
    resolution failure).
    """
    h = await _register(client, "disc_unexp@example.com")
    with patch(
        "services.discussion_service.create_discussion",
        new=AsyncMock(side_effect=RuntimeError("simulated DB failure")),
    ):
        r = await client.post(
            "/api/discussion/sessions",
            headers=h,
            json={
                "topic": "Test topic",
                "rules": "Test rules",
                "persona_ids": ["buffett", "lynch"],
            },
        )
    assert r.status_code == 500
    body = r.json()
    detail = body.get("detail", "")
    assert "RuntimeError" in detail
    assert "simulated DB failure" in detail


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
    # Block the scoreboard service's live-fallback path (direct
    # TWSE + FinMind connector calls) so this test can't reach a
    # real upstream in CI. Without this, the on-demand compute
    # finds zero archived bars, falls through to live, gets
    # today's bar back, and `days_resolved` ends up 1 instead of 0.
    from data.tw import finmind_connector as _fm
    from data.tw import twse_connector as _twse

    async def _no_bars(*args, **kwargs):
        return []

    monkeypatch.setattr(_twse, "get_daily_ohlcv", _no_bars)
    monkeypatch.setattr(_fm, "get_daily_ohlcv", _no_bars)

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
    # Plain (non-debug) call → no debug payload.
    assert body.get("debug") is None


@pytest.mark.asyncio
async def test_get_scoreboard_debug_mode_returns_trace(
    client: AsyncClient, db_session: AsyncSession, monkeypatch,
):
    """`?debug=true` returns the per-symbol diagnostic trace +
    cron eligibility + trading-window resolution so an operator
    can see why a scoreboard came back empty."""
    from data.tw import finmind_connector as _fm
    from data.tw import twse_connector as _twse

    async def _no_bars(*args, **kwargs):
        return []

    monkeypatch.setattr(_twse, "get_daily_ohlcv", _no_bars)
    monkeypatch.setattr(_fm, "get_daily_ohlcv", _no_bars)

    h = await _register(client, "scoreboard_debug@example.com")
    r = await client.post(
        "/api/discussion/sessions",
        headers=h,
        json={"topic": "x", "rules": "y", "persona_ids": ["buffett", "lynch"]},
    )
    discussion_id = r.json()["id"]

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
        f"/api/discussion/sessions/{discussion_id}/scoreboard?debug=true",
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    dbg = body["debug"]
    assert dbg is not None
    assert dbg["discussion"]["recommended_symbols"] == ["2330"]
    assert dbg["discussion"]["daily_close_prices_state"] == "null"
    assert dbg["cron_eligibility"]["eligible"] is True
    assert dbg["cron_eligibility"]["daily_close_missing"] is True
    assert dbg["trading_window"]["window_days_target"] == 5
    # Per-symbol trace: archive empty + live fallback tried (returned []).
    assert len(dbg["per_symbol"]) == 1
    t = dbg["per_symbol"][0]
    assert t["symbol"] == "2330"
    assert t["archive_bars_count"] == 0
    assert t["live_fallback_tried"] is True
    assert t["day1_open_source"] == "none"


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


# ── PR-A1 follow-up: walk-forward HTTP endpoint ────────────────────


async def _create_strategy_via_api(
    client: AsyncClient, headers: dict, *,
    name: str = "WF API test",
) -> str:
    r = await client.post(
        "/api/discussion/strategies",
        headers=headers,
        json={
            "name": name,
            "description": "wf",
            "topic": "topic", "rules": "rules",
            "market": "TW",
            "persona_ids": ["buffett", "lynch"],
            "default_rounds": 1,
            "default_concurrency": 1,
            "default_auto_post_mortem": False,
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _seed_ohlcv_days(
    db: AsyncSession, *, market: str = "TW",
    start: str = "2025-09-01", n: int = 200,
) -> list[str]:
    """Seed `n` consecutive weekday-only OHLCV rows so
    plan_walk_forward can resolve enough trading days. Returns the
    list of ISO dates seeded."""
    from datetime import date, timedelta

    from models.ohlcv_daily import OhlcvDaily

    cur = date.fromisoformat(start)
    dates: list[str] = []
    while len(dates) < n:
        if cur.weekday() < 5:
            dates.append(cur.isoformat())
            db.add(OhlcvDaily(
                market=market, symbol="2330", ts=cur,
                open=100.0, high=101.0, low=99.0, close=100.0,
                volume=1_000_000, source="test",
            ))
        cur = cur + timedelta(days=1)
    await db.commit()
    return dates


@pytest.mark.asyncio
async def test_walk_forward_returns_404_for_unknown_strategy(
    client: AsyncClient,
):
    h = await _register(client, "wf_unknown@example.com")
    r = await client.post(
        f"/api/discussion/strategies/{uuid.uuid4()}/walk-forward",
        headers=h,
        json={
            "anchor_date": "2026-05-05",
            "n_folds": 1,
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_walk_forward_returns_400_for_bad_anchor(
    client: AsyncClient,
):
    h = await _register(client, "wf_bad_anchor@example.com")
    sid = await _create_strategy_via_api(client, h)
    r = await client.post(
        f"/api/discussion/strategies/{sid}/walk-forward",
        headers=h,
        json={
            "anchor_date": "not-a-date",
            "n_folds": 1,
        },
    )
    assert r.status_code == 400
    assert "anchor_date" in r.json()["detail"]


@pytest.mark.asyncio
async def test_walk_forward_returns_400_for_thin_archive(
    client: AsyncClient, db_session: AsyncSession,
):
    """When ohlcv_daily can't reach the requested span, the
    plan-resolver raises ValueError → 400."""
    h = await _register(client, "wf_thin@example.com")
    sid = await _create_strategy_via_api(client, h)
    # Seed only 30 days — far less than the default 60+20 needed
    # for one fold.
    await _seed_ohlcv_days(db_session, n=30)
    r = await client.post(
        f"/api/discussion/strategies/{sid}/walk-forward",
        headers=h,
        json={
            "anchor_date": "2026-05-05",
            "n_folds": 1,
        },
    )
    assert r.status_code == 400
    assert "ohlcv_daily" in r.json()["detail"]


@pytest.mark.asyncio
async def test_walk_forward_kicks_off_and_returns_plan(
    client: AsyncClient, db_session: AsyncSession,
):
    """Happy path — plan resolves, orchestrator detaches into the
    background, response carries the fold layout. The actual
    orchestrator is patched to a no-op so the test doesn't try
    to run real LLM calls."""
    h = await _register(client, "wf_kickoff@example.com")
    sid = await _create_strategy_via_api(client, h)
    seeded = await _seed_ohlcv_days(db_session, n=120)
    anchor = seeded[-1]

    with patch(
        "services.walk_forward_service."
        "execute_walk_forward_in_background",
        return_value=AsyncMock(),
    ) as kickoff:
        r = await client.post(
            f"/api/discussion/strategies/{sid}/walk-forward",
            headers=h,
            json={
                "anchor_date": anchor,
                "train_window_days": 20,
                "test_window_days": 10,
                "n_folds": 2,
            },
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["started"] is True
    assert body["strategy_id"] == sid
    assert body["market"] == "TW"
    assert body["train_window_days"] == 20
    assert body["test_window_days"] == 10
    assert len(body["folds"]) == 2
    # Fold layout: most-recent fold's test ends at anchor, each
    # fold's train ends right before its test starts.
    f0 = body["folds"][0]
    assert len(f0["train_dates"]) == 20
    assert len(f0["test_dates"]) == 10
    assert max(f0["train_dates"]) < min(f0["test_dates"])
    # Orchestrator was scheduled exactly once.
    assert kickoff.call_count == 1


@pytest.mark.asyncio
async def test_walk_forward_owner_scoped_returns_404_for_others(
    client: AsyncClient,
):
    """One user's strategy must be invisible to another's
    walk-forward request — owner-scope is enforced ahead of the
    plan resolver."""
    a = await _register(client, "wf_owner_a@example.com")
    b = await _register(client, "wf_owner_b@example.com")
    sid = await _create_strategy_via_api(client, a)
    r = await client.post(
        f"/api/discussion/strategies/{sid}/walk-forward",
        headers=b,
        json={"anchor_date": "2026-05-05", "n_folds": 1},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_walk_forward_returns_409_when_already_active(
    client: AsyncClient, db_session: AsyncSession,
):
    """Audit follow-up #2: a second walk-forward request for the
    same strategy while the first is still in flight gets 409.
    Without this, two parallel orchestrators would each create
    train+test sweeps and race on weight learning."""
    h = await _register(client, "wf_already_active@example.com")
    sid = await _create_strategy_via_api(client, h)
    seeded = await _seed_ohlcv_days(db_session, n=120)

    # Seed a running train fold for this strategy — simulates an
    # earlier walk-forward run that's still mid-flight.
    from sqlalchemy import select
    from models.backtest_sweep import BacktestSweep
    from models.user import User as _User
    user = (await db_session.execute(
        select(_User).where(_User.email == "wf_already_active@example.com"),
    )).scalar_one()
    db_session.add(BacktestSweep(
        id=uuid.uuid4(), owner_id=user.id,
        topic="t", rules="r", market="TW",
        persona_ids=["bull"],
        anchor_date=__import__(
            "datetime", fromlist=["date"],
        ).date.fromisoformat(seeded[0]),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=uuid.UUID(sid),
        fold_kind="train",
        status="running",
    ))
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/strategies/{sid}/walk-forward",
        headers=h,
        json={
            "anchor_date": seeded[-1],
            "n_folds": 1,
        },
    )
    assert r.status_code == 409
    assert "in flight" in r.json()["detail"]


@pytest.mark.asyncio
async def test_walk_forward_allows_new_run_after_previous_terminal(
    client: AsyncClient, db_session: AsyncSession,
):
    """A completed previous run shouldn't block a new
    walk-forward — operator can re-trigger as much as they want
    once the prior run has terminated."""
    h = await _register(client, "wf_after_done@example.com")
    sid = await _create_strategy_via_api(client, h)
    seeded = await _seed_ohlcv_days(db_session, n=120)

    from sqlalchemy import select
    from models.backtest_sweep import BacktestSweep
    from models.user import User as _User
    user = (await db_session.execute(
        select(_User).where(_User.email == "wf_after_done@example.com"),
    )).scalar_one()
    # Previous run has completed — must not block.
    db_session.add(BacktestSweep(
        id=uuid.uuid4(), owner_id=user.id,
        topic="t", rules="r", market="TW",
        persona_ids=["bull"],
        anchor_date=__import__(
            "datetime", fromlist=["date"],
        ).date.fromisoformat(seeded[0]),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=uuid.UUID(sid),
        fold_kind="train",
        status="completed",
    ))
    await db_session.commit()

    with patch(
        "services.walk_forward_service."
        "execute_walk_forward_in_background",
        return_value=AsyncMock(),
    ):
        r = await client.post(
            f"/api/discussion/strategies/{sid}/walk-forward",
            headers=h,
            json={
                "anchor_date": seeded[-1],
                "train_window_days": 20,
                "test_window_days": 10,
                "n_folds": 1,
            },
        )
    assert r.status_code == 200, r.text


# ── audit Workflow Win #1: brier-history endpoint ──────────────────


@pytest.mark.asyncio
async def test_brier_history_returns_404_for_unknown_strategy(
    client: AsyncClient,
):
    h = await _register(client, "brier_history_404@example.com")
    r = await client.get(
        f"/api/discussion/strategies/{uuid.uuid4()}/brier-history",
        headers=h,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_brier_history_returns_400_for_bad_window(
    client: AsyncClient,
):
    h = await _register(client, "brier_history_window@example.com")
    sid = await _create_strategy_via_api(client, h)
    r = await client.get(
        f"/api/discussion/strategies/{sid}/brier-history?window_days=3",
        headers=h,
    )
    assert r.status_code == 400
    assert "window_days" in r.json()["detail"]


@pytest.mark.asyncio
async def test_brier_history_returns_empty_list_when_no_resolved_sweeps(
    client: AsyncClient,
):
    """Cold-start strategy returns []; the frontend renders a
    "data still warming up" placeholder rather than erroring."""
    h = await _register(client, "brier_history_empty@example.com")
    sid = await _create_strategy_via_api(client, h)
    r = await client.get(
        f"/api/discussion/strategies/{sid}/brier-history",
        headers=h,
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_brier_history_returns_points_for_resolved_sweeps(
    client: AsyncClient, db_session: AsyncSession,
):
    """Happy path — completed sweeps with brier_score on at
    least one discussion show up as ordered datapoints."""
    from datetime import UTC as _UTC, date as _date, datetime as _dt, timedelta as _td

    from sqlalchemy import select
    from models.backtest_sweep import BacktestSweep
    from models.discussion import Discussion as _Disc
    from models.user import User as _User

    h = await _register(client, "brier_history_points@example.com")
    sid = await _create_strategy_via_api(client, h)

    user = (await db_session.execute(
        select(_User).where(_User.email == "brier_history_points@example.com"),
    )).scalar_one()

    sweep = BacktestSweep(
        id=uuid.uuid4(), owner_id=user.id,
        topic="t", rules="r", market="TW",
        persona_ids=["bull"],
        anchor_date=_date(2026, 4, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=uuid.UUID(sid),
        status="completed",
        completed_at=_dt.now(_UTC) - _td(days=2),
    )
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    disc = _Disc(
        id=uuid.uuid4(), owner_id=user.id,
        topic="t", rules="r", persona_ids=["bull"],
        market="TW", status="done", current_round=1,
        sweep_id=sweep.id,
        as_of_date=_date(2026, 4, 1),
        verdict="win",
        brier_score=0.15,
        calibrated_brier_score=0.10,
        outcome_vector=[
            {"symbol": "X", "confidence": 0.8, "outcome_binary": 1},
        ],
    )
    db_session.add(disc)
    await db_session.commit()

    r = await client.get(
        f"/api/discussion/strategies/{sid}/brier-history",
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["sweep_id"] == str(sweep.id)
    assert body[0]["raw_brier"] == pytest.approx(0.15, abs=1e-6)
    assert body[0]["calibrated_brier"] == pytest.approx(0.10, abs=1e-6)
    assert body[0]["samples"] == 1


@pytest.mark.asyncio
async def test_brier_history_owner_scoped_returns_404_for_others(
    client: AsyncClient,
):
    a = await _register(client, "brier_history_owner_a@example.com")
    b = await _register(client, "brier_history_owner_b@example.com")
    sid = await _create_strategy_via_api(client, a)
    r = await client.get(
        f"/api/discussion/strategies/{sid}/brier-history",
        headers=b,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_walk_forward_validates_bounds(
    client: AsyncClient,
):
    """Pydantic schema enforces the bounds — n_folds=99 / window=0
    surfaces as 422 before ever reaching the service."""
    h = await _register(client, "wf_bounds@example.com")
    sid = await _create_strategy_via_api(client, h)
    r = await client.post(
        f"/api/discussion/strategies/{sid}/walk-forward",
        headers=h,
        json={
            "anchor_date": "2026-05-05",
            "n_folds": 99,
        },
    )
    assert r.status_code == 422
    r = await client.post(
        f"/api/discussion/strategies/{sid}/walk-forward",
        headers=h,
        json={
            "anchor_date": "2026-05-05",
            "train_window_days": 0,
        },
    )
    assert r.status_code == 422


# ── B4: interject endpoint (mid-round 插話 + 追問) ─────────────────


async def _create_discussion(client: AsyncClient, headers: dict) -> str:
    create = await client.post(
        "/api/discussion/sessions",
        headers=headers,
        json={"topic": "t", "rules": "r", "persona_ids": ["buffett", "lynch"]},
    )
    assert create.status_code == 201
    return create.json()["id"]


@pytest.mark.asyncio
async def test_interject_queued_while_running(
    client: AsyncClient, db_session: AsyncSession,
):
    """While a round is streaming, POST /interject enqueues the
    question for the round loop and reports `queued` — the actual
    question/answer turns arrive over the round's SSE stream."""
    from services import discussion_service

    h = await _register(client, "disc_interject_run@example.com")
    discussion_id = await _create_discussion(client, h)
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.status = "running"
    row.current_round = 1
    await db_session.commit()

    try:
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/interject",
            headers=h,
            json={"question": "2330 目前的評價?", "target_persona": "lynch"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["target_persona"] == "lynch"

        pending = discussion_service.drain_interjections(
            uuid.UUID(discussion_id),
        )
        assert pending == [
            {"question": "2330 目前的評價?", "target_persona": "lynch"},
        ]
    finally:
        discussion_service.drain_interjections(uuid.UUID(discussion_id))


@pytest.mark.asyncio
async def test_interject_409_when_not_running_nor_concluded(
    client: AsyncClient, db_session: AsyncSession,
):
    """Draft (between rounds) is the classic /inject territory — the
    interject endpoint rejects it with 409 so the two flows stay
    distinct."""
    h = await _register(client, "disc_interject_draft@example.com")
    discussion_id = await _create_discussion(client, h)
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.current_round = 1  # status stays "draft"
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/interject",
        headers=h,
        json={"question": "x"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_interject_404_for_non_owner(
    client: AsyncClient, db_session: AsyncSession,
):
    h_a = await _register(client, "disc_interject_a@example.com")
    h_b = await _register(client, "disc_interject_b@example.com")
    discussion_id = await _create_discussion(client, h_a)
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.status = "running"
    row.current_round = 1
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/interject",
        headers=h_b,
        json={"question": "x"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_interject_400_for_unknown_target_persona(
    client: AsyncClient, db_session: AsyncSession,
):
    h = await _register(client, "disc_interject_badp@example.com")
    discussion_id = await _create_discussion(client, h)
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.status = "running"
    row.current_round = 1
    await db_session.commit()

    r = await client.post(
        f"/api/discussion/sessions/{discussion_id}/interject",
        headers=h,
        json={"question": "x", "target_persona": "not_on_roster"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_interject_followup_on_concluded_returns_answer_turn(
    client: AsyncClient, db_session: AsyncSession,
):
    """追問 path: on a concluded discussion, /interject runs ONE
    synchronous follow-up turn — question + answer both persisted,
    both marked injected_by_user, and surfaced through the session
    detail read."""
    h = await _register(client, "disc_interject_done@example.com")
    discussion_id = await _create_discussion(client, h)
    row = await db_session.scalar(
        select(Discussion).where(Discussion.id == uuid.UUID(discussion_id))
    )
    row.status = "done"
    row.current_round = 1
    row.conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.5,
    }
    await db_session.commit()

    reply = '{"stance": "supplement", "content": "追問回覆：2330 評價仍具吸引力"}'
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events_sequence([reply]),
    ):
        r = await client.post(
            f"/api/discussion/sessions/{discussion_id}/interject",
            headers=h,
            json={"question": "為什麼看好 2330?"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered"

    q = body["question_turn"]
    assert q["persona_id"] == "_user"
    assert q["stance"] == "user_input"
    assert q["injected_by_user"] is True
    assert "2330" in q["content"]

    a = body["answer_turn"]
    # No target named → moderator default = first roster persona.
    assert a["persona_id"] == "buffett"
    assert body["target_persona"] == "buffett"
    assert a["injected_by_user"] is True
    assert a["turn_index"] == q["turn_index"] + 1
    assert "追問回覆" in a["content"]

    # Round-trips through the detail read with the flag intact.
    detail = await client.get(
        f"/api/discussion/sessions/{discussion_id}", headers=h,
    )
    turns = detail.json()["turns"]
    assert [(t["persona_id"], t["injected_by_user"]) for t in turns] == [
        ("_user", True), ("buffett", True),
    ]
