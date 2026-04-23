import redis.asyncio as aioredis
from typing import Optional
from config import settings

_redis: Optional[aioredis.Redis] = None


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


async def cache_delete(key: str) -> None:
    r = await get_redis()
    await r.delete(key)


async def cache_incr(key: str, ttl_seconds: int | None = None) -> int:
    r = await get_redis()
    count = await r.incr(key)
    if ttl_seconds and count == 1:
        await r.expire(key, ttl_seconds)
    return count


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

def key_history(market: str, symbol: str, interval: str) -> str:
    return f"{market}:history:{symbol}:{interval}"

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

def key_ai_counter(user_id: str) -> str:
    return f"ai:requests:{user_id}"

def key_refresh_token(user_id: str, jti: str) -> str:
    return f"refresh:{user_id}:{jti}"

def key_user_sessions(user_id: str) -> str:
    return f"user_sessions:{user_id}"
