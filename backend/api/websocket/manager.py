"""
WebSocket connection manager.

Architecture:
  [APScheduler] → poll quotes → Redis PUBLISH "market:updates"
                                         ↓
  [_pubsub_listener task] → for each connection → filter by symbol → send delta

One Redis Pub/Sub listener runs per process. Each WebSocket connection
registers its subscribed symbols in _subscriptions and receives a callback
when a matching update arrives.

Auth flow:
  1. Client connects to /ws/market
  2. Within 5 seconds, client sends: {"action": "auth", "token": "<jwt>"}
  3. Server validates JWT; closes with 4001 on failure
  4. Client sends: {"action": "subscribe", "symbols": ["AAPL", "2330"]}
  5. Server sends snapshot, then streams deltas via Redis Pub/Sub
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect
from jose import JWTError

from auth.jwt_handler import decode_access_token
from cache.redis_cache import get_redis
from middleware.metrics import (
    WS_CONNECTIONS,
    WS_PUBSUB_DISPATCH_SECONDS,
    WS_PUBSUB_RECONNECTS_TOTAL,
)

# symbol+market → last sent price (for delta detection)
PriceCache = dict[str, float]

# websocket → set of "SYMBOL:MARKET" strings
_subscriptions: dict[WebSocket, set[str]] = {}
# websocket → last-price cache
_last_prices: dict[WebSocket, PriceCache] = {}
# websocket → user_id string (populated on successful auth)
_ws_user: dict[WebSocket, str] = {}
# user_id → set of websockets (multi-tab support)
_user_ws: dict[str, set[WebSocket]] = {}
# websocket → token expiry epoch (re-validated on every message; clients
# can re-auth in-place by sending another {"action":"auth", "token": ...})
_ws_token_exp: dict[WebSocket, float] = {}

_listener_task: asyncio.Task | None = None
_subs_mirror_task: asyncio.Task | None = None
AUTH_TIMEOUT = 5.0       # seconds to send auth message after connect
HEARTBEAT_INTERVAL = 30  # seconds

# ── Cross-process subscription registry ──────────────────────────
# Each web worker mirrors the union of its clients' subscriptions to
# its own Redis key (`ws:subs:<worker_id>`, TTL'd). A dedicated
# scheduler process — which holds zero WebSocket connections — reads
# the union of all workers' keys to decide which symbols to poll.
# The TTL is the liveness contract: a worker that dies simply stops
# refreshing its key and its symbols age out within SUBS_TTL seconds.
_WORKER_ID = uuid.uuid4().hex[:12]
SUBS_KEY_PREFIX = "ws:subs:"
SUBS_TTL = 90            # seconds; refreshed every SUBS_MIRROR_INTERVAL
SUBS_MIRROR_INTERVAL = 30


async def _mirror_subs() -> None:
    """Write this worker's current subscription union to Redis.
    Best-effort: a Redis hiccup here only delays symbol visibility to
    the scheduler by one mirror interval."""
    union: set[str] = set()
    for subs in _subscriptions.values():
        union |= subs
    try:
        r = await get_redis()
        await r.set(
            f"{SUBS_KEY_PREFIX}{_WORKER_ID}",
            json.dumps(sorted(union)),
            ex=SUBS_TTL,
        )
    except Exception:
        pass


async def _subs_mirror_loop() -> None:
    """Keep this worker's registry key alive even when subscriptions
    aren't changing (the TTL must outlive quiet periods)."""
    while True:
        await asyncio.sleep(SUBS_MIRROR_INTERVAL)
        await _mirror_subs()


async def get_global_subscribed(market: str) -> set[str]:
    """Union of symbols subscribed for `market` across ALL workers.

    Falls back to this process's local view when Redis is unreachable —
    degraded (single-worker visibility) beats an empty poll list.
    """
    suffix = f":{market.upper()}"
    try:
        r = await get_redis()
        keys = [k async for k in r.scan_iter(match=f"{SUBS_KEY_PREFIX}*")]
        symbols: set[str] = set()
        if keys:
            for raw in await r.mget(keys):
                if not raw:
                    continue
                try:
                    entries = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                symbols |= {
                    e.rsplit(":", 1)[0]
                    for e in entries
                    if isinstance(e, str) and e.endswith(suffix)
                }
        return symbols
    except Exception:
        local: set[str] = set()
        for subs in _subscriptions.values():
            local |= {e.rsplit(":", 1)[0] for e in subs if e.endswith(suffix)}
        return local


async def _authenticate(ws: WebSocket) -> dict | None:
    """Wait up to AUTH_TIMEOUT for a valid auth message. Returns payload or None."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT)
        msg = json.loads(raw)
        if msg.get("action") != "auth" or not msg.get("token"):
            return None
        payload = decode_access_token(msg["token"])
        return payload
    except (asyncio.TimeoutError, JWTError, json.JSONDecodeError, Exception):
        return None


async def handle_market_ws(ws: WebSocket) -> None:
    await ws.accept()

    # ── Auth ─────────────────────────────────────────────────────
    payload = await _authenticate(ws)
    if not payload:
        await ws.close(code=4001, reason="Authentication failed")
        return

    _subscriptions[ws] = set()
    _last_prices[ws] = {}

    user_id: str = payload.get("sub", "")
    _ws_user[ws] = user_id
    _user_ws.setdefault(user_id, set()).add(ws)
    _ws_token_exp[ws] = float(payload.get("exp") or 0)
    WS_CONNECTIONS.inc()

    heartbeat_task = asyncio.create_task(_heartbeat(ws))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=HEARTBEAT_INTERVAL + 5)
            except asyncio.TimeoutError:
                # Client hasn't sent anything — connection is stale
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = msg.get("action")

            # Allow re-auth (client refreshed access token) without
            # forcing a reconnect. Anything else requires a non-expired
            # token; otherwise close so the client knows to re-auth.
            if action == "auth":
                token = msg.get("token", "")
                try:
                    new_payload = decode_access_token(token)
                except JWTError:
                    await _safe_send(ws, {"type": "error", "code": "auth_failed"})
                    continue
                _ws_token_exp[ws] = float(new_payload.get("exp") or 0)
                await _safe_send(ws, {"type": "auth_ok"})
                continue

            now_epoch = datetime.now(timezone.utc).timestamp()
            if now_epoch >= _ws_token_exp.get(ws, 0):
                await _safe_send(ws, {"type": "error", "code": "token_expired"})
                await ws.close(code=4001, reason="Token expired")
                break

            if action == "subscribe":
                symbols: list[str] = msg.get("symbols", [])
                markets: list[str] = msg.get("markets", [])
                # Build subscription keys
                new_subs: set[str] = set()
                for sym in symbols:
                    for mkt in (markets or ["US"]):
                        new_subs.add(f"{sym.upper()}:{mkt.upper()}")
                _subscriptions[ws] = new_subs
                await _mirror_subs()
                # Send snapshot
                await _send_snapshot(ws, new_subs)

            elif action == "unsubscribe":
                _subscriptions[ws].clear()
                await _mirror_subs()

            elif action == "pong":
                pass  # heartbeat acknowledged

    except WebSocketDisconnect:
        pass
    finally:
        WS_CONNECTIONS.dec()
        heartbeat_task.cancel()
        _subscriptions.pop(ws, None)
        _last_prices.pop(ws, None)
        _ws_token_exp.pop(ws, None)
        uid = _ws_user.pop(ws, None)
        if uid and uid in _user_ws:
            _user_ws[uid].discard(ws)
            if not _user_ws[uid]:
                del _user_ws[uid]
        await _mirror_subs()


async def _send_snapshot(ws: WebSocket, subs: set[str]) -> None:
    """Fetch latest cached quote for each subscription and send as snapshot.

    Single MGET round-trip — a 20-symbol watchlist used to pay 20
    sequential Redis RTTs right at the subscribe moment.
    """
    from cache.redis_cache import cache_mget, key_quote

    sub_keys: list[str] = []
    redis_keys: list[str] = []
    for sub_key in subs:
        parts = sub_key.split(":", 1)
        if len(parts) != 2:
            continue
        symbol, market = parts
        sub_keys.append(sub_key)
        redis_keys.append(key_quote(market.lower(), symbol))

    snapshot: dict = {}
    for sub_key, cached in zip(sub_keys, await cache_mget(redis_keys)):
        if cached:
            try:
                snapshot[sub_key] = json.loads(cached)
            except json.JSONDecodeError:
                continue  # one corrupt entry must not sink the snapshot

    await _safe_send(ws, {"type": "snapshot", "data": snapshot})


async def _heartbeat(ws: WebSocket) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        ts = datetime.now(timezone.utc).isoformat()
        ok = await _safe_send(ws, {"type": "ping", "ts": ts})
        if not ok:
            break


async def _safe_send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_text(json.dumps(data))
        return True
    except Exception:
        return False


# ── Redis Pub/Sub listener ────────────────────────────────────────

PUBSUB_CHANNEL = "market:updates"
ALERTS_CHANNEL = "user:alerts"


async def start_pubsub_listener() -> None:
    """
    Runs as a long-lived background task.
    Subscribes to Redis channels and dispatches market deltas + user
    alerts to connected WebSockets. Also starts the subscription-mirror
    refresher so this worker's symbols stay visible to the scheduler
    process.
    """
    global _listener_task, _subs_mirror_task
    _listener_task = asyncio.create_task(_listen_loop())
    _subs_mirror_task = asyncio.create_task(_subs_mirror_loop())


async def _listen_loop() -> None:
    """Outer supervision loop: `pubsub.listen()` simply ends when the
    Redis connection drops, which used to kill this task silently and
    leave every WebSocket client without deltas until a process
    restart. Reconnect with capped exponential backoff instead — the
    subscribe happens after every reconnect so no re-registration is
    needed elsewhere.
    """
    backoff = 1.0
    while True:
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(PUBSUB_CHANNEL, ALERTS_CHANNEL)
            try:
                backoff = 1.0  # healthy subscribe → reset the ladder
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        payload: dict = json.loads(message["data"])
                        if message.get("channel") == ALERTS_CHANNEL:
                            await _deliver_alert_local(
                                payload.get("user_id", ""),
                                payload.get("data", {}),
                            )
                        else:
                            await _dispatch(payload)
                    except Exception:
                        continue
            finally:
                try:
                    await pubsub.unsubscribe(PUBSUB_CHANNEL, ALERTS_CHANNEL)
                except Exception:
                    pass  # connection already gone — nothing to leave behind
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        # listen() returning OR raising both mean the subscription is dead.
        WS_PUBSUB_RECONNECTS_TOTAL.inc()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


async def _dispatch(payload: dict) -> None:
    """
    payload = {"symbol": "AAPL", "market": "US", "price": 182.5, "change_pct": 0.4, "ts": ...}
    Send delta to every WebSocket subscribed to this symbol:market.
    Only send if price changed by more than 0.01%.
    """
    sub_key = f"{payload['symbol']}:{payload['market']}"
    dead: list[WebSocket] = []
    now_epoch = datetime.now(timezone.utc).timestamp()
    dispatch_started = time.monotonic()

    for ws, subs in list(_subscriptions.items()):
        if sub_key not in subs:
            continue
        if now_epoch >= _ws_token_exp.get(ws, 0):
            # Token expired — stop pushing passive updates. The next
            # client message will close the socket cleanly.
            continue

        last = _last_prices[ws].get(sub_key)
        price = payload.get("price", 0)
        if last is not None and abs(price - last) / (last or 1) < 0.0001:
            continue   # change < 0.01% — skip to reduce noise

        _last_prices[ws][sub_key] = price
        ok = await _safe_send(ws, {"type": "delta", "symbol": payload["symbol"],
                                    "market": payload["market"], "data": payload})
        if not ok:
            dead.append(ws)

    for ws in dead:
        _subscriptions.pop(ws, None)
        _last_prices.pop(ws, None)

    WS_PUBSUB_DISPATCH_SECONDS.observe(time.monotonic() - dispatch_started)


async def publish_update(symbol: str, market: str, data: dict) -> None:
    """Called by polling tasks to push a price update into the Pub/Sub bus."""
    r = await get_redis()
    payload = {"symbol": symbol, "market": market, **data}
    await r.publish(PUBSUB_CHANNEL, json.dumps(payload))


async def publish_alert_to_user(user_id: str, data: dict) -> None:
    """Publish a fired alert onto the shared alerts channel.

    This is the impl registered with notification_service in EVERY
    process (web workers and the dedicated scheduler): the fire site
    publishes once, and each web worker's pubsub listener delivers to
    whatever sockets that worker holds. Fixes the pre-existing gap
    where an alert fired on worker A never reached a user whose tab
    was connected to worker B.
    """
    r = await get_redis()
    await r.publish(ALERTS_CHANNEL, json.dumps({"user_id": user_id, "data": data}))


# Kept under its historical name for callers/tests that push directly
# to this process's sockets; the pubsub listener is the only
# production call site.
async def _deliver_alert_local(user_id: str, data: dict) -> None:
    """Send an alert to all WebSocket connections THIS process owns for user_id."""
    connections = list(_user_ws.get(user_id, set()))
    dead: list[WebSocket] = []
    now_epoch = datetime.now(timezone.utc).timestamp()
    for ws in connections:
        if now_epoch >= _ws_token_exp.get(ws, 0):
            continue   # don't push to sockets whose access token has expired
        ok = await _safe_send(ws, data)
        if not ok:
            dead.append(ws)
    for ws in dead:
        _subscriptions.pop(ws, None)
        _last_prices.pop(ws, None)
        _ws_token_exp.pop(ws, None)
        uid = _ws_user.pop(ws, None)
        if uid and uid in _user_ws:
            _user_ws[uid].discard(ws)
            if not _user_ws[uid]:
                del _user_ws[uid]
