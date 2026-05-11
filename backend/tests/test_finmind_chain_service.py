"""Unit tests for `services.finmind_chain_service`.

The chain service uses redis for state, lock, and stop flag. We mock
redis with a dict so the test exercises the real code path
(`_read_state` → JSON deserialize → mutation → JSON serialize) but
keeps everything in-memory. The downstream `ingest_chunk` is stubbed
to avoid the FinMind HTTP path."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def fake_redis(monkeypatch):
    """Replace the autouse mock_redis with a dict-backed fake so
    cache_set values survive into cache_get calls within the same
    test. Returns the dict for direct manipulation."""
    store: dict[str, str] = {}

    async def _get(key: str):
        return store.get(key)

    async def _set(key: str, value: str, ttl_seconds: int):
        store[key] = value

    async def _delete(key: str):
        store.pop(key, None)

    # Build a mock redis client matching the methods chain service uses
    # via cache.redis_cache helpers (acquire_lock + _refresh_lock +
    # _release_lock all hit get_redis().get / .set / .delete directly).
    redis_client = AsyncMock()

    async def _client_get(key: str):
        return store.get(key)

    async def _client_set(key: str, value: str, **kwargs):
        # Honour SET NX semantics for acquire_lock.
        if kwargs.get("nx") and key in store:
            return None
        store[key] = value
        return True

    async def _client_delete(*keys: str):
        n = 0
        for k in keys:
            if k in store:
                store.pop(k)
                n += 1
        return n

    redis_client.get = _client_get
    redis_client.set = _client_set
    redis_client.delete = _client_delete
    redis_client.incr.return_value = 1

    monkeypatch.setattr("cache.redis_cache.cache_get", _get)
    monkeypatch.setattr("cache.redis_cache.cache_set", _set)
    monkeypatch.setattr("cache.redis_cache.cache_delete", _delete)
    monkeypatch.setattr(
        "cache.redis_cache.get_redis", AsyncMock(return_value=redis_client)
    )
    # Also patch the imports re-exported at the chain service module level.
    monkeypatch.setattr(
        "services.finmind_chain_service.cache_get", _get
    )
    monkeypatch.setattr(
        "services.finmind_chain_service.cache_set", _set
    )
    monkeypatch.setattr(
        "services.finmind_chain_service.cache_delete", _delete
    )
    monkeypatch.setattr(
        "services.finmind_chain_service.get_redis",
        AsyncMock(return_value=redis_client),
    )

    yield store


@pytest_asyncio.fixture
async def stub_helpers(monkeypatch):
    """Patch the DB-touching helpers (universe, host-chain detection,
    quota gauge, stuck-reset, dataset counts) with deterministic
    fakes. Lets the service tests focus on state + control flow."""
    fakes: dict[str, Any] = {
        "universe": ["2330", "2454", "2317"],
        "host_chain_active": False,
        "quota_used": 0,
        "quota_limit": 6000,
        "quota_limit_global": 6000,
        "stuck_reset_count": 0,
        "dataset_progress": {},
    }

    async def _fake_universe():
        return list(fakes["universe"])

    async def _fake_host_chain():
        return fakes["host_chain_active"]

    async def _fake_quota_used():
        return fakes["quota_used"]

    async def _fake_quota_limit():
        return fakes["quota_limit"]

    async def _fake_quota_limit_global():
        return fakes["quota_limit_global"]

    async def _fake_reset_stuck():
        return fakes["stuck_reset_count"]

    async def _fake_count_progress_one(ds: str):
        # Test fixture stores per-dataset (done, failed) tuples; the
        # service now also tracks total. Synthesize total = done +
        # failed unless an explicit 3-tuple is provided.
        rec = fakes["dataset_progress"].get(ds, (0, 0))
        if len(rec) == 3:
            return rec
        done, failed = rec
        return (done, failed, done + failed)

    async def _fake_count_progress_many(datasets):
        done = 0
        total = 0
        for d in datasets:
            rec = fakes["dataset_progress"].get(d, (0, 0))
            if len(rec) == 3:
                done += rec[0]
                total += rec[2]
            else:
                done += rec[0]
                total += rec[0] + rec[1]
        return (done, total)

    async def _fake_per_dataset_progress(datasets, universe_size):
        return []

    monkeypatch.setattr(
        "services.finmind_chain_service._load_universe", _fake_universe
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._host_chain_likely_active",
        _fake_host_chain,
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._quota_used", _fake_quota_used
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._quota_limit", _fake_quota_limit
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._quota_limit_global",
        _fake_quota_limit_global,
    )
    monkeypatch.setattr(
        "services.finmind_chain_service.reset_stuck", _fake_reset_stuck
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._count_progress_for_dataset",
        _fake_count_progress_one,
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._count_progress_for_datasets",
        _fake_count_progress_many,
    )
    monkeypatch.setattr(
        "services.finmind_chain_service._per_dataset_progress",
        _fake_per_dataset_progress,
    )
    yield fakes


@pytest_asyncio.fixture
async def stub_ingest_chunk(monkeypatch):
    """Replace `finmind.ingest.runner.ingest_chunk` with a recordable
    stub. Returns the call list and a `set_result` helper."""
    from dataclasses import dataclass

    @dataclass
    class _Result:
        status: str
        rows_written: int
        error: str | None

    calls: list[dict] = []
    state = {"result": _Result("done", 100, None)}

    async def _fake_ingest_chunk(session, **kwargs):
        calls.append(kwargs)
        return state["result"]

    monkeypatch.setattr(
        "finmind.ingest.runner.ingest_chunk", _fake_ingest_chunk
    )
    # Also patch FinmindAsyncSessionLocal — chain service opens a
    # session per chunk via this. Use a dummy async ctx manager so the
    # stub doesn't try to talk to a real DB.
    class _DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr(
        "services.finmind_chain_service.FinmindAsyncSessionLocal",
        lambda: _DummySession(),
        raising=False,
    )
    # The chain service does `from finmind.db.session import
    # FinmindAsyncSessionLocal` inside _run_chain. Patch the import
    # site by replacing the attribute on that module too.
    monkeypatch.setattr(
        "finmind.db.session.FinmindAsyncSessionLocal",
        lambda: _DummySession(),
    )
    yield {"calls": calls, "state": state}


@pytest.mark.asyncio
async def test_get_state_initial_is_idle(fake_redis, stub_helpers):
    from services.finmind_chain_service import get_state

    state = await get_state()
    assert state["status"] == "idle"
    assert state["queue"] == []
    assert state["chunks_done"] == 0
    assert state["external_activity_detected"] is False
    # Idle chain: no selected datasets yet → totals are 0
    assert state["total_chunks_done"] == 0
    assert state["total_chunks_total"] == 0
    assert state["quota_limit"] == 6000
    assert "TaiwanStockPriceAdj" in state["default_datasets"]


@pytest.mark.asyncio
async def test_start_chain_refuses_when_host_chain_detected(
    fake_redis, stub_helpers,
):
    from services.finmind_chain_service import ChainConflict, start_chain

    stub_helpers["host_chain_active"] = True
    with pytest.raises(ChainConflict):
        await start_chain(["TaiwanStockPriceAdj"], days=30)


@pytest.mark.asyncio
async def test_start_chain_refuses_when_lock_held(fake_redis, stub_helpers):
    from services.finmind_chain_service import (
        ChainAlreadyRunning, LOCK_KEY, start_chain,
    )

    fake_redis[LOCK_KEY] = "other-instance:42"
    with pytest.raises(ChainAlreadyRunning):
        await start_chain(["TaiwanStockPriceAdj"], days=30)


@pytest.mark.asyncio
async def test_start_chain_rejects_empty_dataset_list(
    fake_redis, stub_helpers,
):
    from services.finmind_chain_service import start_chain

    with pytest.raises(ValueError):
        await start_chain([], days=30)


@pytest.mark.asyncio
async def test_full_chain_run_completes_and_marks_idle(
    fake_redis, stub_helpers, stub_ingest_chunk,
):
    """Drive a complete chain through one dataset × 3 symbols. After
    the run loop exits naturally, state should be back to idle, lock
    released, all chunks recorded."""
    from services.finmind_chain_service import (
        LOCK_KEY, get_state, start_chain,
    )

    state = await start_chain(["TaiwanStockPriceAdj"], days=30)
    assert state["status"] == "running"

    # Wait for the background task to drain (3 symbols × 1 dataset).
    for _ in range(40):
        s = await get_state()
        if s["status"] == "idle":
            break
        await asyncio.sleep(0.05)

    s = await get_state()
    assert s["status"] == "idle"
    # When idle the live per-dataset bar is hidden (zeroed) — overall
    # progress lives in total_chunks_done. The stub fakes
    # _count_progress_for_datasets, so total_done reflects what we
    # primed in `dataset_progress` (empty here → 0).
    assert s["chunks_done"] == 0
    assert s["chunks_total"] == 0
    assert LOCK_KEY not in fake_redis  # released

    assert len(stub_ingest_chunk["calls"]) == 3
    assert {c["symbol"] for c in stub_ingest_chunk["calls"]} == {
        "2330", "2454", "2317",
    }


@pytest.mark.asyncio
async def test_stop_chain_observes_flag_after_current_chunk(
    fake_redis, stub_helpers, monkeypatch,
):
    """Soft-stop semantics: set flag, current chunk finishes, chain
    exits. We arrange the universe to be 5 symbols and call stop after
    the first chunk lands; the chain should stop before the 5th."""
    from services.finmind_chain_service import (
        get_state, start_chain, stop_chain, STOP_KEY,
    )

    stub_helpers["universe"] = ["A", "B", "C", "D", "E"]
    chunk_calls: list[dict] = []
    stop_after_first = asyncio.Event()

    async def _slow_ingest(session, **kwargs):
        from dataclasses import dataclass

        @dataclass
        class _R:
            status: str = "done"
            rows_written: int = 1
            error: str | None = None

        chunk_calls.append(kwargs)
        if len(chunk_calls) == 1:
            stop_after_first.set()
        await asyncio.sleep(0.02)
        return _R()

    monkeypatch.setattr(
        "finmind.ingest.runner.ingest_chunk", _slow_ingest
    )

    class _DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr(
        "finmind.db.session.FinmindAsyncSessionLocal",
        lambda: _DummySession(),
    )

    await start_chain(["TaiwanStockPriceAdj"], days=30)
    await stop_after_first.wait()
    await stop_chain()
    assert STOP_KEY in fake_redis  # flag is set

    # Wait for the chain to exit
    for _ in range(40):
        s = await get_state()
        if s["status"] == "idle":
            break
        await asyncio.sleep(0.05)

    s = await get_state()
    assert s["status"] == "idle"
    # Stop kicked in after 1+ chunks but before all 5
    assert 1 <= len(chunk_calls) < 5


@pytest.mark.asyncio
async def test_chain_continues_when_chunk_fails(
    fake_redis, stub_helpers, monkeypatch,
):
    """A single failed chunk (e.g. quota-exhausted, network error) must
    NOT kill the chain. The runner already swallows internal exceptions
    into ChunkResult(status='failed', ...), but a session-level error
    can still bubble up. Verify both paths leave the chain running."""
    from services.finmind_chain_service import get_state, start_chain
    from dataclasses import dataclass

    @dataclass
    class _R:
        status: str
        rows_written: int = 0
        error: str | None = None

    call_count = {"n": 0}

    async def _flaky_ingest(session, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return _R("failed", 0, "FinMindQuotaExhausted: 5234/5000")
        return _R("done", 1)

    monkeypatch.setattr(
        "finmind.ingest.runner.ingest_chunk", _flaky_ingest
    )

    class _DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    monkeypatch.setattr(
        "finmind.db.session.FinmindAsyncSessionLocal",
        lambda: _DummySession(),
    )

    await start_chain(["TaiwanStockPriceAdj"], days=30)
    for _ in range(40):
        s = await get_state()
        if s["status"] == "idle":
            break
        await asyncio.sleep(0.05)

    s = await get_state()
    assert s["status"] == "idle"
    # Idle path zeroes the live counters — assert via recent_errors
    # that the failed chunk still got recorded for operator visibility.
    assert s["chunks_done"] == 0
    assert s["chunks_total"] == 0
    assert any(
        "FinMindQuotaExhausted" in e for e in s["recent_errors"]
    ), s["recent_errors"]


@pytest.mark.asyncio
async def test_get_state_recovers_from_corrupted_json(
    fake_redis, stub_helpers,
):
    """A malformed redis blob shouldn't poison subsequent reads — the
    service should fall back to a fresh idle state. `stub_helpers` is
    needed alongside `fake_redis` because get_state() calls
    `_host_chain_likely_active` which executes Postgres-specific
    `interval` SQL that SQLite (the CI test DB) can't parse."""
    from services.finmind_chain_service import STATE_KEY, get_state

    fake_redis[STATE_KEY] = "{not-valid-json"
    state = await get_state()
    assert state["status"] == "idle"


@pytest.mark.asyncio
async def test_get_state_heals_orphaned_running_state(
    fake_redis, stub_helpers,
):
    """Backend restart kills the in-process `_run_chain` task without
    running its finally block, leaving redis state stuck on
    running/stopping. With no lock held, get_state() should self-heal
    to idle so the UI doesn't show 正在停止… forever."""
    from services.finmind_chain_service import (
        LOCK_KEY,
        STATE_KEY,
        STOP_KEY,
        ChainState,
        get_state,
    )

    orphan = ChainState(
        status="stopping",
        queue=["TaiwanStockPriceAdj"],
        current_dataset="TaiwanStockMarginPurchaseShortSale",
        current_symbol="03098B",
        chunks_done=12604,
        chunks_total=126465,
        stop_requested=True,
        started_at="2026-05-07T07:26:02+00:00",
        last_chunk_at="2026-05-07T07:43:52+00:00",
    )
    fake_redis[STATE_KEY] = orphan.to_json()
    fake_redis[STOP_KEY] = "1"
    assert LOCK_KEY not in fake_redis  # lock TTL has expired

    state = await get_state()

    assert state["status"] == "idle"
    assert state["current_dataset"] is None
    assert state["current_symbol"] is None
    assert state["queue"] == []
    assert state["stop_requested"] is False
    # STOP_KEY removed so a future start_chain doesn't inherit it.
    assert STOP_KEY not in fake_redis
    # Healed state was persisted, not just returned.
    persisted = ChainState.from_json(fake_redis[STATE_KEY])
    assert persisted.status == "idle"


@pytest.mark.asyncio
async def test_get_state_does_not_heal_when_lock_held(
    fake_redis, stub_helpers,
):
    """A live worker holds the lock — must not be reset to idle just
    because get_state() is called mid-run."""
    from services.finmind_chain_service import (
        LOCK_KEY,
        STATE_KEY,
        ChainState,
        get_state,
    )

    live = ChainState(
        status="running",
        queue=["TaiwanStockPriceAdj"],
        current_dataset="TaiwanStockPriceAdj",
        current_symbol="2330",
        chunks_done=5,
    )
    fake_redis[STATE_KEY] = live.to_json()
    fake_redis[LOCK_KEY] = "worker-A:123"

    state = await get_state()

    assert state["status"] == "running"
    assert state["current_dataset"] == "TaiwanStockPriceAdj"


@pytest.mark.asyncio
async def test_get_state_overrides_chunks_from_ledger_when_running(
    fake_redis, stub_helpers,
):
    """Regression: redis-persisted `chunks_total = len(universe)` plus
    `chunks_done` baseline-from-ledger let the per-dataset bar exceed
    100% (TaiwanStockMarginPurchaseShortSale showed 127933/126465 on
    2026-05-10). When the chain is running, get_state() must rebuild
    chunks_done/failed/total from the ledger so done ≤ total always."""
    from services.finmind_chain_service import (
        LOCK_KEY,
        STATE_KEY,
        ChainState,
        get_state,
    )

    stub_helpers["dataset_progress"] = {
        # Same shape as the prod incident: ledger has done=127933,
        # failed=154609, pending=313 (total=282855); the persisted
        # chain state still has the buggy len(universe) baseline.
        "TaiwanStockMarginPurchaseShortSale": (127933, 154609, 282855),
    }
    live = ChainState(
        status="running",
        queue=[],
        selected_datasets=["TaiwanStockMarginPurchaseShortSale"],
        current_dataset="TaiwanStockMarginPurchaseShortSale",
        current_symbol="2330",
        chunks_done=127933,
        chunks_total=126465,   # the buggy len(universe) baseline
        chunks_failed=154609,
        universe_size=126465,
    )
    fake_redis[STATE_KEY] = live.to_json()
    fake_redis[LOCK_KEY] = "worker-A:123"

    state = await get_state()

    assert state["chunks_done"] == 127933
    assert state["chunks_failed"] == 154609
    assert state["chunks_total"] == 282855
    assert state["chunks_done"] <= state["chunks_total"]
    # Overall bar should agree with ledger sum, not universe × N.
    assert state["total_chunks_done"] == 127933
    assert state["total_chunks_total"] == 282855


@pytest.mark.asyncio
async def test_get_state_zeros_chunks_when_idle(
    fake_redis, stub_helpers,
):
    """Idle chain: the live per-dataset bar is hidden by zeroing
    `chunks_*`. Without this, a stale `chunks_done=127933,
    chunks_total=126465` (the literal 2026-05-10 incident shape)
    persists in redis and the front-end keeps rendering 101% across
    sessions even when no dataset is being processed. Overall
    progress still flows through `total_chunks_done/total` from the
    ledger helper."""
    from services.finmind_chain_service import (
        STATE_KEY,
        ChainState,
        get_state,
    )

    stub_helpers["dataset_progress"] = {
        "TaiwanStockMarginPurchaseShortSale": (127933, 154609, 282855),
    }
    persisted = ChainState(
        status="idle",
        queue=[],
        selected_datasets=["TaiwanStockMarginPurchaseShortSale"],
        current_dataset=None,
        current_symbol=None,
        chunks_done=127933,
        chunks_total=126465,   # the buggy len(universe) baseline
        chunks_failed=154609,
    )
    fake_redis[STATE_KEY] = persisted.to_json()

    state = await get_state()

    assert state["status"] == "idle"
    assert state["chunks_done"] == 0
    assert state["chunks_total"] == 0
    assert state["chunks_failed"] == 0
    # Overall totals still come from the ledger helper.
    assert state["total_chunks_done"] == 127933
    assert state["total_chunks_total"] == 282855


@pytest.mark.asyncio
async def test_state_serialises_via_to_json_round_trip():
    """Sanity check: the dataclass round-trips through JSON without
    losing fields (defends against accidental Field rename in service)."""
    from services.finmind_chain_service import ChainState

    state = ChainState(
        status="running",
        queue=["A", "B"],
        current_dataset="A",
        current_symbol="2330",
        chunks_done=42,
        chunks_total=100,
        chunks_failed=3,
        started_at="2026-05-07T12:00:00+00:00",
        last_chunk_at="2026-05-07T12:01:00+00:00",
        stop_requested=False,
        recent_errors=["foo", "bar"],
    )
    raw = state.to_json()
    parsed = ChainState.from_json(raw)
    assert parsed == state
    # Verify the JSON shape is what /chain returns (no extra keys
    # beyond the dataclass fields — augmented fields come from
    # get_state).
    obj = json.loads(raw)
    assert set(obj.keys()) == set(state.__dict__.keys())
