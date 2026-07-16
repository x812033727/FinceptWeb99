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

