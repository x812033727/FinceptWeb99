import uuid
from datetime import UTC, datetime

import pytest

from config import settings
from models.discussion import Discussion, DiscussionTurn
from models.user import User, UserRole


def discussion(owner_id, *, created_at, conclusion, auto_run=True, status="done"):
    return Discussion(
        id=uuid.uuid4(), owner_id=owner_id, topic="今日選股", rules="internal",
        persona_ids=["market_analyst", "risk_manager"], market="TW",
        status=status, current_round=5, conclusion=conclusion,
        auto_run=auto_run, created_at=created_at, updated_at=created_at,
    )


@pytest.mark.asyncio
async def test_public_daily_disabled_without_auth(client, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "")
    response = await client.get("/api/public/daily")
    assert response.status_code == 200
    assert response.json()["state"] == "disabled"
    assert response.headers["cache-control"].startswith("public, max-age=60")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


@pytest.mark.asyncio
async def test_public_daily_selects_latest_valid_auto_run_and_redacts(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="Publisher@Example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    other = User(id=uuid.uuid4(), email="other@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add_all([owner, other])
    await db_session.flush()
    valid = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion={
        "recommended_symbols": ["2330"], "reasoning": "公開理由", "risks": ["波動"],
        "time_horizon": "short_term", "consensus_score": .8,
        "captured_session": {"session_date": "2026-07-13", "phase": "close"},
        "lessons": ["secret"], "internal_context": {"secret": True},
    })
    db_session.add_all([
        valid,
        discussion(owner.id, created_at=datetime(2026, 7, 15, tzinfo=UTC), conclusion={"_parse_error": True, "reasoning": "bad"}),
        discussion(owner.id, created_at=datetime(2026, 7, 16, tzinfo=UTC), conclusion={"reasoning": "manual"}, auto_run=False),
        discussion(other.id, created_at=datetime(2026, 7, 17, tzinfo=UTC), conclusion={"reasoning": "other"}),
    ])
    await db_session.flush()
    db_session.add_all([
        DiscussionTurn(discussion_id=valid.id, round=1, turn_index=0, persona_id="market_analyst", stance="agree", content="公開發言"),
        DiscussionTurn(discussion_id=valid.id, round=2, turn_index=0, persona_id="risk_manager", stance="dissent", content="手動回覆", injected_by_user=True),
        DiscussionTurn(discussion_id=valid.id, round=5, turn_index=0, persona_id="risk_manager", stance="supplement", content="第五輪"),
        DiscussionTurn(discussion_id=valid.id, round=6, turn_index=0, persona_id="risk_manager", stance="supplement", content="事後輪次"),
        DiscussionTurn(discussion_id=valid.id, round=5, turn_index=1, persona_id="_system:discussion_synthesizer", stance="supplement", content="系統"),
    ])
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "publisher@example.com")

    response = await client.get("/api/public/daily")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["result"]["conclusion"]["reasoning"] == "公開理由"
    assert body["result"]["captured_session"]["session_date"] == "2026-07-13"
    assert [t["content"] for t in body["result"]["turns"]] == ["公開發言", "第五輪"]
    serialized = response.text
    for forbidden in ("Publisher@", "internal_context", "secret", "手動回覆", "事後輪次", "post_mortem", "rules"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_public_daily_keeps_retired_strategy_groups_visible(client, db_session, monkeypatch):
    """Rows written before the 5→3 strategy merge carry retired keys
    (e.g. "breakout"); the day grouping must still surface them next to
    the merged keys instead of dropping them from the payload."""
    owner = User(id=uuid.uuid4(), email="pub@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": ["波動"],
        "time_horizon": "short_term", "consensus_score": .8,
    }
    legacy = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=conclusion)
    legacy.auto_run_strategy = "breakout"
    legacy.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    db_session.add(legacy)
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "pub@example.com")

    response = await client.get("/api/public/daily")
    assert response.status_code == 200
    body = response.json()
    day = body["days"][0]
    for key in ("general", "chip_quality", "price_signal"):
        assert key in day["strategies"]
    assert [r["strategy"] for r in day["strategies"]["breakout"]] == ["breakout"]


@pytest.mark.asyncio
async def test_public_daily_passes_candidate_pool_through(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="pub@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    }
    pool = [{"symbol": "2330", "strategy_score": 12.3, "signal_type": "breakout"}]
    with_pool = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=conclusion)
    with_pool.auto_run_strategy = "price_signal"
    with_pool.auto_run_sequence = 1
    with_pool.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    with_pool.candidate_snapshot = {"strategy": "price_signal", "sequence": 1, "candidates": [], "pool": pool}
    without_pool = discussion(owner.id, created_at=datetime(2026, 7, 14, 1, tzinfo=UTC), conclusion=conclusion)
    without_pool.auto_run_strategy = "price_signal"
    without_pool.auto_run_sequence = 2
    without_pool.auto_run_date = with_pool.auto_run_date
    without_pool.candidate_snapshot = {"strategy": "price_signal", "sequence": 2, "candidates": []}
    db_session.add_all([with_pool, without_pool])
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    runs = body["days"][0]["strategies"]["price_signal"]
    assert [r["candidate_pool"] for r in runs] == [pool, []]


@pytest.mark.asyncio
async def test_public_daily_shows_seven_recent_days(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="pub@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    }
    for day in range(8, 16):  # 8 distinct run dates
        row = discussion(owner.id, created_at=datetime(2026, 7, day, tzinfo=UTC), conclusion=conclusion)
        row.auto_run_strategy = "general"
        row.auto_run_date = datetime(2026, 7, day, tzinfo=UTC).date()
        db_session.add(row)
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    dates = [d["date"] for d in body["days"]]
    assert len(dates) == 7
    assert dates == sorted(dates, reverse=True)
    assert "2026-07-08" not in dates  # oldest of the 8 rolls off


@pytest.mark.asyncio
async def test_public_daily_serves_cached_payload(client, monkeypatch):
    async def fake_get(key):
        return {
            "state": "ready", "strategies": {}, "days": [],
            "result": None, "disclaimer": "cached!",
        }

    monkeypatch.setattr("api.public_daily.cache_get_json", fake_get)
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    assert body["disclaimer"] == "cached!"


@pytest.mark.asyncio
async def test_public_daily_writes_cache_on_miss(client, monkeypatch):
    calls = []

    async def fake_set(key, value, ttl_seconds):
        calls.append((key, value, ttl_seconds))

    monkeypatch.setattr("api.public_daily.cache_set_json", fake_set)
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    assert body["state"] == "empty"  # owner doesn't exist
    assert len(calls) == 1
    key, value, ttl = calls[0]
    assert value["state"] == "empty"
    assert ttl == 60


@pytest.mark.asyncio
async def test_public_daily_window_anchored_on_newest_row(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="pub@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    }
    fresh = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=conclusion)
    fresh.auto_run_strategy = "general"
    fresh.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    # Way outside QUERY_WINDOW_DAYS of the newest row — bounded scan
    # must not surface it even though only 2 run dates exist.
    stale = discussion(owner.id, created_at=datetime(2026, 1, 5, tzinfo=UTC), conclusion=conclusion)
    stale.auto_run_strategy = "general"
    stale.auto_run_date = datetime(2026, 1, 5, tzinfo=UTC).date()
    db_session.add_all([fresh, stale])
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    dates = [d["date"] for d in body["days"]]
    assert dates == ["2026-07-14"]

