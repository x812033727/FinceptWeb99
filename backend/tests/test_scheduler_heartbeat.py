"""APScheduler liveness probe — heartbeat write + read tests.

Covers the contract between `tasks.scheduler_heartbeat.write_heartbeat`
and `services.scheduler_health.read_heartbeat`:
  - happy-path round-trip writes a parseable JSON blob
  - read tolerates missing key / malformed JSON / missing ts field
  - `age_seconds` reflects wall-clock delta vs the written timestamp
  - `stale` flips at the 60s threshold
  - Prometheus gauge reflects age (or -1 sentinel when missing)
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from middleware.metrics import SCHEDULER_HEARTBEAT_AGE_SECONDS
from services.scheduler_health import (
    STALE_THRESHOLD_SECONDS,
    read_heartbeat,
)
from tasks.scheduler_heartbeat import (
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    write_heartbeat,
)


@pytest.mark.asyncio
async def test_write_heartbeat_emits_iso_timestamp_and_version():
    captured = {}

    async def _fake_set(key: str, value: str, ttl: int):
        captured["key"] = key
        captured["value"] = value
        captured["ttl"] = ttl

    with patch("tasks.scheduler_heartbeat.cache_set", _fake_set):
        await write_heartbeat()

    assert captured["key"] == HEARTBEAT_KEY
    assert captured["ttl"] == HEARTBEAT_TTL_SECONDS
    # JSON-decodable, has ts + version keys
    import json
    payload = json.loads(captured["value"])
    assert "ts" in payload
    assert "version" in payload
    # ts parses as an aware ISO timestamp
    parsed = datetime.fromisoformat(payload["ts"])
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_write_heartbeat_swallows_redis_outage():
    """Redis SET failure must not propagate — the next 30s tick will
    retry, and meanwhile the read side surfaces "stale" via TTL."""
    with patch(
        "tasks.scheduler_heartbeat.cache_set",
        AsyncMock(side_effect=RuntimeError("redis down")),
    ):
        # No exception raised
        await write_heartbeat()


@pytest.mark.asyncio
async def test_read_heartbeat_returns_stale_when_key_missing():
    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(return_value=None),
    ):
        snap = await read_heartbeat()

    assert snap.last_beat_at is None
    assert snap.age_seconds is None
    assert snap.stale is True
    assert snap.version is None
    assert snap.ttl_seconds == HEARTBEAT_TTL_SECONDS


@pytest.mark.asyncio
async def test_read_heartbeat_returns_stale_on_malformed_json():
    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(return_value="not-json"),
    ):
        snap = await read_heartbeat()

    assert snap.stale is True
    assert snap.last_beat_at is None
    assert snap.age_seconds is None


@pytest.mark.asyncio
async def test_read_heartbeat_returns_fresh_when_recent():
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    payload = f'{{"ts":"{fresh.isoformat()}","version":"0.5.84"}}'

    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(return_value=payload),
    ):
        snap = await read_heartbeat()

    assert snap.last_beat_at == fresh.isoformat()
    assert snap.age_seconds is not None
    assert 0 <= snap.age_seconds < 30
    assert snap.stale is False
    assert snap.version == "0.5.84"


@pytest.mark.asyncio
async def test_read_heartbeat_returns_stale_past_threshold():
    """A heartbeat older than STALE_THRESHOLD_SECONDS (60s) but still
    within the Redis TTL window (180s) reads back as stale=True so
    the AdminPage can warn before the key actually disappears."""
    old = datetime.now(UTC) - timedelta(seconds=STALE_THRESHOLD_SECONDS + 5)
    payload = f'{{"ts":"{old.isoformat()}","version":"0.5.84"}}'

    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(return_value=payload),
    ):
        snap = await read_heartbeat()

    assert snap.stale is True
    assert snap.age_seconds is not None
    assert snap.age_seconds > STALE_THRESHOLD_SECONDS


@pytest.mark.asyncio
async def test_read_heartbeat_falls_open_on_redis_outage():
    """When Redis itself is unreachable, return the missing-key shape
    rather than raising — keeps the health endpoint available."""
    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(side_effect=RuntimeError("redis down")),
    ):
        snap = await read_heartbeat()

    assert snap.stale is True
    assert snap.last_beat_at is None


@pytest.mark.asyncio
async def test_read_heartbeat_sets_prometheus_gauge_to_age():
    fresh = datetime.now(UTC) - timedelta(seconds=12)
    payload = f'{{"ts":"{fresh.isoformat()}","version":"0.5.84"}}'

    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(return_value=payload),
    ):
        await read_heartbeat()

    age = SCHEDULER_HEARTBEAT_AGE_SECONDS._value.get()
    assert 10 <= age <= 14


@pytest.mark.asyncio
async def test_read_heartbeat_sets_gauge_to_minus_one_when_missing():
    """The -1 sentinel distinguishes "scheduler has never beat" from
    "beat 0 seconds ago" — alerting rules need to be able to match
    the fully-dead case."""
    with patch(
        "services.scheduler_health.cache_get",
        AsyncMock(return_value=None),
    ):
        await read_heartbeat()

    assert SCHEDULER_HEARTBEAT_AGE_SECONDS._value.get() == -1.0
