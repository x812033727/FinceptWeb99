import time
import redis.asyncio as aioredis
from typing import Optional
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

def key_ai_counter(user_id: str) -> str:
    return f"ai:requests:{user_id}"

def key_refresh_token(user_id: str, jti: str) -> str:
    return f"refresh:{user_id}:{jti}"

def key_user_sessions(user_id: str) -> str:
    return f"user_sessions:{user_id}"

def key_github_release() -> str:
    return "github:release:latest"
