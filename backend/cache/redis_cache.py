import json
import time
import redis.asyncio as aioredis
from typing import Any, Optional
from config import settings

_redis: Optional[aioredis.Redis] = None
_token_bucket_sha: Optional[str] = None


class RedisUnavailable(Exception):
    """Raised when a Redis operation fails so the caller can use a local fallback."""


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def cache_get(key: str) -> Optional[str]:
    r = await get_redis()
    return await r.get(key)


async def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    r = await get_redis()
    await r.set(key, value, ex=ttl_seconds)


async def cache_get_json(key: str) -> Any | None:
    """Read cached JSON. Returns the parsed value on hit, None on miss
    OR on malformed cache entry (decode error). The decode-error path
    is intentional: one corrupted Redis value should make the call site
    refetch upstream rather than 500 the request — services call this
    in the hot path of every quote / history / fundamentals read.
    """
    cached = await cache_get(key)
    if cached is None:
        return None
    try:
        return json.loads(cached)
    except (json.JSONDecodeError, TypeError):
        return None


async def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    """Serialize `value` and store it under `key` with the given TTL.
    Folds the `json.dumps + cache_set` pair that's repeated across
    every service's get_quote / get_history / get_X path."""
    await cache_set(key, json.dumps(value), ttl_seconds)


async def cache_set_json_unless_empty(
    key: str, value: Any, ttl_seconds: int,
) -> None:
    """Same as `cache_set_json`, but skip the write when `value` is
    falsy (None, empty list / dict / str / 0). Mirrors the
    "don't cache empty" guard that tw / us / portfolio services
    repeat by hand so a transient upstream failure can't lock the
    next request into 15 s of an empty payload.

    For the "permanent empty" case (this symbol genuinely has no
    broker breakdown), wrap the sentinel in a single-key dict so
    truthiness still flags it as cacheable.
    """
    if not value:
        return
    await cache_set_json(key, value, ttl_seconds)


async def cache_set_unless_empty(
    key: str, payload: str | None, ttl_seconds: int,
) -> None:
    """Cache `payload` under `key`, skipping the write when payload is None.

    Two policies for "no upstream data" co-exist across the services and
    look identical at the call site but mean different things:

    - **Transient empty** (yfinance blip, TWSE timeout, FinMind 503):
      pass ``payload=None`` so the next request retries. Mirrors the
      "don't cache empty" guard in tw_market_service / us_market_service.
    - **Permanent empty** (this symbol genuinely has no broker
      breakdown, no upcoming events in the lookahead window): pre-
      serialize a sentinel and pass that — subsequent requests hit
      cache and return None fast without burning a fetch.

    Failures are swallowed. Call-sites previously wrapped cache_set in
    try/except by hand; folding it into the helper keeps the policy
    explicit and the call site short.
    """
    if payload is None:
        return
    try:
        await cache_set(key, payload, ttl_seconds)
    except Exception:
        pass


async def cache_delete(key: str) -> None:
    r = await get_redis()
    await r.delete(key)


async def cache_incr(key: str, ttl_seconds: int | None = None) -> int:
    r = await get_redis()
    count = await r.incr(key)
    if ttl_seconds and count == 1:
        await r.expire(key, ttl_seconds)
    return count


async def cache_decr(key: str, min_value: int = 0) -> int:
    """Decrement a counter, never falling below `min_value`.

    Used to refund quota counters when a request fails before producing
    usable output (see ai_agents router refund logic).
    """
    r = await get_redis()
    val = await r.decr(key)
    try:
        ival = int(val)
    except (TypeError, ValueError):
        return min_value
    if ival < min_value:
        await r.set(key, min_value)
        return min_value
    return ival


# ── Distributed token bucket (rate limiter) ──────────────────────
# Atomic refill + acquire in one Redis round-trip via Lua. Used to
# enforce a global rate limit across all uvicorn workers / k8s pods —
# something asyncio.Semaphore can't do because it's process-local.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
    tokens = capacity
    ts = now
end

local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 60)
return allowed
"""


async def acquire_token(key: str, capacity: float, rate: float) -> bool:
    """Try to acquire one token from a Redis-backed token bucket.

    Returns True if a token was acquired, False if the bucket is empty.
    Raises `RedisUnavailable` when Redis fails so the caller can fall back
    to a local pacing strategy.

    Args:
        key: Redis key holding the bucket state (HMSET: tokens, ts).
        capacity: Maximum number of tokens the bucket holds.
        rate: Refill rate in tokens per second.
    """
    global _token_bucket_sha
    now_ms = int(time.time() * 1000)
    try:
        r = await get_redis()
        if _token_bucket_sha is None:
            loaded = await r.script_load(_TOKEN_BUCKET_LUA)
            _token_bucket_sha = loaded if isinstance(loaded, str) else None
        if _token_bucket_sha:
            result = await r.evalsha(_token_bucket_sha, 1, key, capacity, rate, now_ms)
        else:
            result = await r.eval(_TOKEN_BUCKET_LUA, 1, key, capacity, rate, now_ms)
        return int(result) == 1
    except Exception as exc:
        raise RedisUnavailable(str(exc)) from exc


async def acquire_lock(key: str, ttl_seconds: int, value: str = "1") -> bool:
    """SET NX with TTL — used by scheduler tasks to coordinate across pods.

    Returns True if this caller acquired the lock. The TTL acts as a
    self-healing safety net: if the holder crashes without releasing, the
    lock auto-expires.
    """
    try:
        r = await get_redis()
        # SET key value NX EX ttl — atomic
        result = await r.set(key, value, nx=True, ex=ttl_seconds)
        return bool(result)
    except Exception:
        return False


async def release_lock(key: str) -> None:
    try:
        r = await get_redis()
        await r.delete(key)
    except Exception:
        pass


async def ping() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception:
        return False


# ── Key builders ─────────────────────────────────────────────────
# Convention: {market}:{datatype}:{symbol}:{granularity}

def key_quote(market: str, symbol: str) -> str:
    return f"{market}:quote:{symbol}:realtime"

def key_history(market: str, symbol: str, interval: str, range_token: str = "") -> str:
    """`range_token` distinguishes requests that share an `interval` but
    cover different ranges (e.g. US `3mo` and `1y` both use `interval=1d`,
    TW `1mo / 3mo / 1y / 5y` all use the daily archive). Without it, the
    cache for one range serves the wrong data to the next range. Default
    empty string preserves the previous key shape for callers that pass
    a unique interval per range (e.g. crypto)."""
    suffix = f":{range_token}" if range_token else ""
    return f"{market}:history:{symbol}:{interval}{suffix}"

def key_fundamentals(market: str, symbol: str) -> str:
    return f"{market}:fundamentals:{symbol}:snapshot"

def key_institutional(symbol: str) -> str:
    return f"tw:institutional:{symbol}:daily"

def key_margin(symbol: str) -> str:
    return f"tw:margin:{symbol}:daily"

def key_revenue(symbol: str) -> str:
    return f"tw:revenue:{symbol}:monthly"

def key_finmind_counter() -> str:
    return "finmind:daily_requests"

def key_finmind_quota_exhausted_counter() -> str:
    """Hourly counter for quota-overrun events. Bumped each time a
    backfill/scheduler call enters `quota_strict()` and the local
    counter has overshot `FINMIND_HOURLY_REQUEST_LIMIT`. Surfaced in
    the admin status report so operators notice when their parallel
    backfills are saturating the FinMind cap."""
    return "finmind:quota_exhausted:hourly"

def key_finmind_ip_banned() -> str:
    """Circuit-breaker flag set when FinMind responds with HTTP 403 +
    body `{"msg": "ip banned", "retry_after": NNNN}`. Stored value is
    the upstream `retry_after` seconds; TTL is `retry_after + 60s`
    (capped to 1h) so the flag self-clears. While present, every
    `_query` call short-circuits without contacting FinMind — this is
    the whole point: each request during a ban risks resetting /
    extending FinMind's countdown via their abuse detector."""
    return "finmind:ip_banned"

def key_ai_counter(user_id: str) -> str:
    return f"ai:requests:{user_id}"

def key_refresh_token(user_id: str, jti: str) -> str:
    return f"refresh:{user_id}:{jti}"

def key_user_sessions(user_id: str) -> str:
    return f"user_sessions:{user_id}"

def key_github_release() -> str:
    return "github:release:latest"
