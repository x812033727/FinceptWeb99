import json
import time
from typing import Any

import redis.asyncio as aioredis

from config import settings

_redis: aioredis.Redis | None = None
_token_bucket_sha: str | None = None


class RedisUnavailable(Exception):
    """Raised when a Redis operation fails so the caller can use a local fallback."""


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def cache_get(key: str) -> str | None:
    r = await get_redis()
    return await r.get(key)


async def cache_mget(keys: list[str]) -> list[str | None]:
    """Batch read — one round-trip for N keys, order-preserving,
    missing keys come back as None. Used by the WebSocket snapshot
    path, which previously issued one GET per subscribed symbol."""
    if not keys:
        return []
    r = await get_redis()
    return await r.mget(keys)


async def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    r = await get_redis()
    await r.set(key, value, ex=ttl_seconds)


def _endpoint_label(key: str) -> str:
    """Derive a `{market}.{datatype}` Prometheus label from the cache
    key's first two colon-delimited segments — keeps the label-set
    bounded (one per cache-key shape) instead of exploding with one
    per symbol. Empty key falls back to `"unknown"`, and a key with
    only one segment (e.g. a top-level helper key) collapses to that
    segment alone so it still shows up on the scrape rather than
    being dropped.
    """
    if not key:
        return "unknown"
    parts = key.split(":", 2)
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}.{parts[1]}"
    return parts[0] or "unknown"


async def cache_get_json(key: str) -> Any | None:
    """Read cached JSON. Returns the parsed value on hit, None on miss
    OR on malformed cache entry (decode error). The decode-error path
    is intentional: one corrupted Redis value should make the call site
    refetch upstream rather than 500 the request — services call this
    in the hot path of every quote / history / fundamentals read.

    Emits `cache_hits_total{endpoint=...}` / `cache_misses_total{...}`
    via the Prometheus registry so operators can see hit-rate per
    cache-key shape on the existing `/metrics` scrape.
    """
    # Lazy-import so a cold pytest collection that only touches the
    # cache helper doesn't pull starlette / prometheus_client until it
    # actually needs to record a counter.
    from middleware.metrics import CACHE_HITS_TOTAL, CACHE_MISSES_TOTAL

    cached = await cache_get(key)
    endpoint = _endpoint_label(key)
    if cached is None:
        CACHE_MISSES_TOTAL.labels(endpoint=endpoint).inc()
        return None
    try:
        result = json.loads(cached)
    except (json.JSONDecodeError, TypeError):
        # A poisoned cache entry surfaces as a miss to the caller; mirror
        # that semantic in the metric so "miss rate" reflects the
        # operator's "how often did we refetch upstream" mental model.
        CACHE_MISSES_TOTAL.labels(endpoint=endpoint).inc()
        return None
    CACHE_HITS_TOTAL.labels(endpoint=endpoint).inc()
    return result


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


async def cache_hincrby(
    key: str, field: str, amount: int = 1, ttl_seconds: int | None = None,
) -> int:
    """Increment a field inside a Redis hash by `amount`, returning the
    new field value. When `ttl_seconds` is set the whole hash's expiry
    is (re)armed on the field's first creation only — mirrors
    `cache_incr`'s "expire on first bump" semantics so a per-day usage
    hash self-clears without every write extending its life.

    Used by the FinMind per-dataset upstream usage counter: one hash
    per UTC day, one field per dataset_code."""
    r = await get_redis()
    count = await r.hincrby(key, field, amount)
    if ttl_seconds and count == amount:
        # First time this field appears in the hash — arm the TTL. A
        # brand-new hash has exactly one field whose value == amount, so
        # this fires once per day-key rather than on every dataset call.
        await r.expire(key, ttl_seconds)
    return count


async def cache_hgetall(key: str) -> dict[str, str]:
    """Read an entire Redis hash as a `{field: value}` dict (empty when
    the key is missing). Used by the usage-report script to pull one
    day's per-dataset call counts in a single round-trip."""
    r = await get_redis()
    return await r.hgetall(key)


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

def key_intraday(market: str, symbol: str, interval: str) -> str:
    """Snapshot-aggregated intraday bars (A2 分時) — interval ∈ {1m, 5m, 15m}."""
    return f"{market}:intraday:{symbol}:{interval}"

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

def key_finmind_dataset_usage(day: str) -> str:
    """Per-day Redis hash of FinMind upstream calls broken down by
    dataset_code. `day` is a UTC `YYYYMMDD` stamp; each field is a
    dataset_code and its value the number of outbound calls that day.

    The global `key_finmind_counter` answers "how close are we to the
    hourly cap right now" but has no dataset dimension, so it can't tell
    operators WHICH datasets are burning the FinMind budget. This hash
    fills that gap; `finmind.scripts.usage_report` aggregates the last
    N day-keys into a ranked table to drive the self-crawl cutover
    priority order. TTL is 35 days (set by the connector on first write
    each day) so a fortnight-plus of history is always queryable while
    old keys self-expire."""
    return f"finmind:upstream:usage:{day}"

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


# ── US-specific cache key builders ─────────────────────────────────
# Moved out of `us_market_service` so `grep "f\"us:"` finds zero hits
# and every key prefix is auditable in one file.

def key_financials_us(ticker: str) -> str:
    """Annual income / balance / cash-flow statements (Polygon → yfinance)."""
    return f"us:financials:{ticker}:annual"


def key_options_us(ticker: str, expiration_date: str | None = None) -> str:
    """Option chain. `expiration_date=None` means "all expirations bundled"."""
    return f"us:options:{ticker}:{expiration_date or 'all'}"


def key_news_us(ticker: str) -> str:
    """Google News RSS (en-US) fallback to yfinance.news."""
    return f"us:news:{ticker.upper()}"


def key_earnings_us(ticker: str) -> str:
    """Next earnings date + EPS / revenue consensus (yfinance.calendar)."""
    return f"us:earnings:{ticker.upper()}"


def key_macro_us(name: str, as_of: str | None = None) -> str:
    """FRED macro series (fed_funds_rate / cpi / gdp / yield_curve / …).
    `as_of` (ISO date) namespaces backtest reads so they don't poison
    the live cache."""
    if as_of is not None:
        return f"us:macro:{name}:asof={as_of}"
    return f"us:macro:{name}"


def key_screener_us(
    *,
    min_market_cap: float | None,
    min_pe: float | None,
    max_pe: float | None,
    min_pb: float | None,
    max_pb: float | None,
    min_dividend_yield: float | None,
    min_volume: int | None,
    sector: str | None,
    limit: int,
) -> str:
    """Composite filter key for the live US screener path. Backtest
    (`as_of`) shape is a separate concern handled inside the service."""
    return (
        f"us:screener:{min_market_cap}:{min_pe}:{max_pe}:{min_pb}:{max_pb}:"
        f"{min_dividend_yield}:{min_volume}:{sector}:{limit}"
    )


# ── TW-specific cache key builders ─────────────────────────────────

def key_fundamentals_tw(symbol: str) -> str:
    """TW fundamentals — distinct from the generic ``key_fundamentals``
    (which suffixes ``:snapshot`` for US). The TW path landed earlier
    without the suffix and we preserve that shape so this PR is a
    pure refactor (no cache invalidation)."""
    return f"tw:fundamentals:{symbol}"


def key_financials_tw(symbol: str) -> str:
    """TW income / balance / cash-flow statements (FinMind)."""
    return f"tw:financials:{symbol}"


def key_news_tw(symbol: str) -> str:
    """Google News RSS (zh-TW) backed TW symbol news."""
    return f"tw:news:{symbol.upper()}"


def key_valuation_band_tw(symbol: str, metric: str, years: int) -> str:
    """本益比 / 股價淨值比 河流圖 — metric ∈ {pe, pb}."""
    return f"tw:valuation_band:{symbol}:{metric}:{years}"


def key_dividends_tw(symbol: str) -> str:
    """Historical dividends (FinMind)."""
    return f"tw:dividends:{symbol}"


def key_etf_holdings_tw(symbol: str) -> str:
    """ETF underlying holdings (FinMind / TWSE)."""
    return f"tw:etf_holdings:{symbol}"


def key_health_tw(symbol: str, periods: int = 8) -> str:
    """Versioned TW statement-analysis key, including response window."""
    return f"tw:health:v2:{symbol}:{periods}"


def key_factor_ranking_tw(
    as_of: str, profile: str, limit: int, sector_neutral: bool = True,
    model_key: str | None = None,
) -> str:
    """Explainable multi-factor ranking, versioned by methodology."""
    base = f"tw:factor_ranking:v8:{as_of}:{profile}:{limit}:{int(sector_neutral)}"
    return f"{base}:{model_key}" if model_key else base


def key_factor_validation_tw(
    start_date: str, end_date: str, profile: str, top_n: int,
    holding_sessions: int, transaction_cost_bps: float,
    sector_neutral: bool = True,
    portfolio_notional_twd: float = 10_000_000,
    max_participation_rate: float = 0.05,
    impact_coefficient_bps: float = 10,
    benchmark: str = "taiex_total_return",
    weight_mode: str = "walk_forward",
) -> str:
    """Rolling factor validation with every result-shaping input in the key."""
    return (
        f"tw:factor_validation:v8:{start_date}:{end_date}:{profile}:{top_n}:"
        f"{holding_sessions}:{transaction_cost_bps:g}:{int(sector_neutral)}:"
        f"{portfolio_notional_twd:g}:{max_participation_rate:g}:"
        f"{impact_coefficient_bps:g}:{benchmark}:{weight_mode}"
    )


def key_archive_last2_tw(symbol: str) -> str:
    """Last 2 (date, close) pairs from the ohlcv_daily archive — feeds
    the prev_close resolver. See `tw_prev_close`."""
    return f"tw:archive_last2:{symbol}"


def key_prev_close_finmind_tw(symbol: str) -> str:
    """FinMind-tier prev_close fallback (10-day window). See `tw_prev_close`."""
    return f"tw:prev_close_finmind:{symbol}"


def key_screener_tw(
    *,
    exchange: str | None,
    min_volume: int | None,
    min_pe: float | None,
    max_pe: float | None,
    min_pb: float | None,
    max_pb: float | None,
    min_dividend_yield: float | None,
    include_etf: bool,
    etf_only: bool,
    limit: int,
    ohlcv_tag: str,
) -> str:
    """Composite filter key for the live TW screener path. `ohlcv_tag`
    is the latest archived session ISO date (or `"none"`) so backtest
    and live results don't collide."""
    return (
        f"tw:screener:{exchange}:{min_volume}:"
        f"{min_pe}:{max_pe}:{min_pb}:{max_pb}:{min_dividend_yield}:"
        f"{include_etf}:{etf_only}:{limit}:{ohlcv_tag}"
    )
