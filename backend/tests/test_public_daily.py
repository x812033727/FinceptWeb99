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
async def test_build_response_direct(db_session):
    """Direct-call coverage of the projection builder — the HTTP tests
    exercise it too, but the coverage tracer doesn't reliably see lines
    run inside the ASGI dispatch."""
    from api.public_daily import _build_response

    owner = User(id=uuid.uuid4(), email="direct@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    idle = User(id=uuid.uuid4(), email="idle@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add_all([owner, idle])
    await db_session.flush()
    conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    }
    row = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=conclusion)
    row.auto_run_strategy = "general"
    row.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    db_session.add(row)
    await db_session.flush()
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="market_analyst", stance="agree", content="直呼發言",
    ))
    await db_session.commit()

    # Unknown email → empty; known publisher with no discussions → empty.
    assert (await _build_response(db_session, "ghost@example.com")).state == "empty"
    assert (await _build_response(db_session, "idle@example.com")).state == "empty"

    resp = await _build_response(db_session, "direct@example.com")
    assert resp.state == "ready"
    assert [d.date for d in resp.days] == ["2026-07-14"]
    run = resp.days[0].strategies["general"][0]
    assert [t.content for t in run.turns] == ["直呼發言"]
    assert resp.result is not None and resp.result.topic == run.topic


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


@pytest.mark.asyncio
async def test_public_daily_ignores_backtest_replays(client, db_session, monkeypatch):
    """A replay row is stamped with today's `created_at` but recommends for a
    past session. It sorts to the top of `created_at desc` and would be served
    as the current day's picks — which is what a fuel sweep did in production.
    The anchor must come from the live row even when a replay is newer.
    """
    owner = User(id=uuid.uuid4(), email="bt-pub@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()

    def _conclusion(sym):
        return {
            "recommended_symbols": [sym], "reasoning": "理由", "risks": [],
            "time_horizon": "short_term", "consensus_score": .8,
        }

    live = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC),
                      conclusion=_conclusion("2330"))
    live.auto_run_strategy = "general"
    live.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    # Replayed later (newer created_at) but anchored on a June session.
    replay = discussion(owner.id, created_at=datetime(2026, 7, 20, tzinfo=UTC),
                        conclusion=_conclusion("1101"))
    replay.auto_run_strategy = "general"
    replay.auto_run_date = datetime(2026, 6, 12, tzinfo=UTC).date()
    replay.as_of_date = datetime(2026, 6, 12, tzinfo=UTC).date()
    db_session.add_all([live, replay])
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "bt-pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    assert body["state"] == "ready"
    assert body["result"]["conclusion"]["recommended_symbols"] == ["2330"]
    assert [d["date"] for d in body["days"]] == ["2026-07-14"]


@pytest.mark.asyncio
async def test_public_scoreboard_endpoint(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="sb-pub@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    row = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion={"reasoning": "x"})
    row.auto_run_strategy = "general"
    row.verdict = "win"
    row.day1_open_prices = {"2330": 100.0}
    row.day5_close_prices = {"2330": 106.0}
    db_session.add(row)
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "sb-pub@example.com")

    body = (await client.get("/api/public/daily/scoreboard")).json()
    assert body["state"] == "ready"
    entry = body["entries"][0]
    assert entry["strategy"] == "general"
    assert entry["win_rate"] == 1.0
    assert entry["avg_return_pct"] == 6.0

    # Direct-call the handler path pieces for tracer-proof coverage.
    from services.daily_scoreboard_service import build_scoreboard
    entries = await build_scoreboard(db_session, owner.id)
    assert entries[0]["strategy"] == "general"


@pytest.mark.asyncio
async def test_public_scoreboard_disabled_and_empty(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "")
    assert (await client.get("/api/public/daily/scoreboard")).json()["state"] == "disabled"

    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "ghost-sb@example.com")
    assert (await client.get("/api/public/daily/scoreboard")).json()["state"] == "empty"


@pytest.mark.asyncio
async def test_public_scoreboard_serves_cache_and_writes_it(client, monkeypatch):
    calls = []

    async def fake_set(key, value, ttl_seconds):
        calls.append((key, value, ttl_seconds))

    monkeypatch.setattr("api.public_daily.cache_set_json", fake_set)
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "ghost-sb2@example.com")
    body = (await client.get("/api/public/daily/scoreboard")).json()
    assert body["state"] == "empty"
    scoreboard_calls = [c for c in calls if "scoreboard" in c[0]]
    assert len(scoreboard_calls) == 1 and scoreboard_calls[0][2] == 300

    async def fake_get(key):
        return {"state": "ready", "entries": [], "disclaimer": "cached-sb"}

    monkeypatch.setattr("api.public_daily.cache_get_json", fake_get)
    body = (await client.get("/api/public/daily/scoreboard")).json()
    assert body["disclaimer"] == "cached-sb"


@pytest.mark.asyncio
async def test_public_daily_enriches_candidates_with_company_names(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="names@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    conclusion = {
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    }
    row = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=conclusion)
    row.auto_run_strategy = "price_signal"
    row.auto_run_sequence = 1
    row.candidate_snapshot = {
        "strategy": "price_signal", "sequence": 1,
        "candidates": [{"symbol": "2330", "strategy_score": 9.0, "signal_type": "breakout"}],
        "pool": [
            {"symbol": "2330", "strategy_score": 9.0, "signal_type": "breakout"},
            {"symbol": "9999", "strategy_score": 1.0},  # unknown → no name key
        ],
    }
    db_session.add(row)
    await db_session.commit()

    fake_names = {"2330": "台積電"}
    monkeypatch.setattr(
        "api.public_daily.resolve_display_name",
        lambda market, symbol: fake_names.get(symbol),
    )
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "names@example.com")

    body = (await client.get("/api/public/daily")).json()
    run = body["days"][0]["strategies"]["price_signal"][0]
    assert run["candidates"][0]["name"] == "台積電"
    pool_by_symbol = {p["symbol"]: p for p in run["candidate_pool"]}
    assert pool_by_symbol["2330"]["name"] == "台積電"
    assert "name" not in pool_by_symbol["9999"]


@pytest.mark.asyncio
async def test_public_daily_history_serves_symbols_on_the_page(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="hist@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    row = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion={
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    })
    row.candidate_snapshot = {
        "strategy": "general", "sequence": 1,
        "candidates": [{"symbol": "2330", "strategy_score": 9.0}],
        "pool": [{"symbol": "2317", "strategy_score": 5.0}],
    }
    db_session.add(row)
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "hist@example.com")

    calls = []

    async def fake_history(symbol, months=12):
        calls.append((symbol, months))
        return [
            {"time": "2026-07-13", "open": 1000.0, "high": 1010.0, "low": 990.0,
             "close": 1005.0, "volume": 12345, "data_source": "ohlcv_daily"},
            {"time": "2026-07-14", "open": 1005.0, "high": 1020.0, "low": 1000.0,
             "close": 1015.0, "volume": None, "data_source": "ohlcv_daily"},
        ]

    monkeypatch.setattr("services.tw_market_service.get_history", fake_history)

    # Recommended symbol works…
    response = await client.get("/api/public/daily/history/2330")
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("public, max-age=300")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    bars = response.json()
    assert [b["time"] for b in bars] == ["2026-07-13", "2026-07-14"]
    assert bars[0]["close"] == 1005.0
    # data_source is internal — the public projection must not leak it,
    # and a NULL volume becomes 0 for the chart.
    assert "data_source" not in bars[0]
    assert bars[1]["volume"] == 0

    # …and so does a pool-only symbol.
    assert (await client.get("/api/public/daily/history/2317")).status_code == 200
    assert calls == [("2330", 6), ("2317", 6)]


@pytest.mark.asyncio
async def test_public_daily_history_rejects_symbols_off_the_page(client, db_session, monkeypatch):
    owner = User(id=uuid.uuid4(), email="hist2@example.com", hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    db_session.add(discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion={
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": .8,
    }))
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "hist2@example.com")

    async def explode(symbol, months=12):  # pragma: no cover - guard must fire first
        raise AssertionError("get_history must not be reached for off-page symbols")

    monkeypatch.setattr("services.tw_market_service.get_history", explode)

    assert (await client.get("/api/public/daily/history/9999")).status_code == 404


@pytest.mark.asyncio
async def test_public_daily_history_404_when_disabled_or_empty(client, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "")
    assert (await client.get("/api/public/daily/history/2330")).status_code == 404

    # Enabled but publisher has no rows → still 404, never a data probe.
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "nobody@example.com")
    assert (await client.get("/api/public/daily/history/2330")).status_code == 404


@pytest.mark.asyncio
async def test_public_daily_carries_server_computed_tier(client, db_session, monkeypatch):
    """Every serialized run carries a server-computed `tier` — the
    frontend must never re-derive it (single source of truth:
    services.daily_pick_tier.tier_for)."""
    owner = User(id=uuid.uuid4(), email="tier-pub@example.com", hashed_password="x",
                 role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    await db_session.flush()
    strong = {
        "recommended_symbols": ["2330"], "reasoning": "理由", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.95,
        "quality_signals": {"hallucination_warnings": []},
    }
    weak = dict(strong, consensus_score=0.5, recommended_symbols=["1101"])
    abstain = dict(strong, recommended_symbols=[])
    d1 = discussion(owner.id, created_at=datetime(2026, 7, 14, tzinfo=UTC), conclusion=strong)
    d1.auto_run_strategy = "price_signal"
    d1.auto_run_date = datetime(2026, 7, 14, tzinfo=UTC).date()
    d2 = discussion(owner.id, created_at=datetime(2026, 7, 14, 1, tzinfo=UTC), conclusion=weak)
    d2.auto_run_strategy = "general"
    d2.auto_run_date = d1.auto_run_date
    d3 = discussion(owner.id, created_at=datetime(2026, 7, 14, 2, tzinfo=UTC), conclusion=abstain)
    d3.auto_run_strategy = "chip_quality"
    d3.auto_run_date = d1.auto_run_date
    db_session.add_all([d1, d2, d3])
    await db_session.commit()
    monkeypatch.setattr(settings, "PUBLIC_DAILY_RESULTS_OWNER_EMAIL", "tier-pub@example.com")

    body = (await client.get("/api/public/daily")).json()
    flat = {r["strategy"]: r["tier"]
            for runs in body["strategies"].values() for r in runs}
    assert flat["price_signal"] == "recommend"
    assert flat["general"] == "watch"
    assert flat["chip_quality"] is None
    assert body["result"]["tier"] in ("recommend", "watch", None)
