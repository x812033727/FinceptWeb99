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
from jwt import InvalidTokenError as JWTError

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
# reverse index: "SYMBOL:MARKET" → websockets subscribed to it. Keeps
# _dispatch at O(subscribers-of-symbol) instead of scanning every
# connection per tick. Maintained by _index_replace/_prune_socket —
# writer failure and handler cleanup both go through those.
_symbol_subs: dict[str, set[WebSocket]] = {}
# per-connection outbound queue + writer task. Dispatch never awaits a
# client's socket directly: a slow consumer only backs up its own
# bounded queue (oldest delta dropped — stale quotes are worthless),
# never the fan-out loop.
_send_queues: dict[WebSocket, asyncio.Queue] = {}
_writer_tasks: dict[WebSocket, asyncio.Task] = {}
SEND_QUEUE_MAX = 64
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


def _index_replace(ws: WebSocket, old: set[str], new: set[str]) -> None:
    """Swap ws's reverse-index membership from `old` keys to `new` keys."""
    for key in old - new:
        subs = _symbol_subs.get(key)
        if subs is not None:
            subs.discard(ws)
            if not subs:
                _symbol_subs.pop(key, None)
    for key in new - old:
        _symbol_subs.setdefault(key, set()).add(ws)


def _prune_socket(ws: WebSocket) -> None:
    """Remove a dead socket from every registry (idempotent). The
    handler's finally block does the authoritative cleanup; this exists
    so the writer task and dispatch can stop routing to a corpse
    immediately instead of waiting for the receive loop to notice."""
    _index_replace(ws, _subscriptions.pop(ws, set()), set())
    _last_prices.pop(ws, None)
    _ws_token_exp.pop(ws, None)
    _send_queues.pop(ws, None)
    task = _writer_tasks.pop(ws, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()   # self-prune from the writer loop just returns
    uid = _ws_user.pop(ws, None)
    if uid and uid in _user_ws:
        _user_ws[uid].discard(ws)
        if not _user_ws[uid]:
            del _user_ws[uid]


async def _writer_loop(ws: WebSocket, queue: asyncio.Queue) -> None:
    """Single writer per connection: drains the bounded queue in order.
    One slow client stalls only its own queue; a send failure prunes
    the socket from the fan-out registries immediately."""
    while True:
        try:
            item = await queue.get()
        except asyncio.CancelledError:
            return              # cancelled by _prune_socket
        try:
            await ws.send_text(item)
        except asyncio.CancelledError:
            return
        except Exception:
            _prune_socket(ws)
            return


def _send_text(ws: WebSocket, text: str) -> bool:
    """Enqueue pre-serialized text for a managed socket. Drops the
    OLDEST queued message when full — for market data, newest wins.
    Returns False when the socket has no writer (already pruned)."""
    queue = _send_queues.get(ws)
    if queue is None:
        return False
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(text)
    return True


async def _authenticate(ws: WebSocket) -> dict | None:
    """Wait up to AUTH_TIMEOUT for a valid auth message. Returns payload or None."""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT)
        msg = json.loads(raw)
        if msg.get("action") != "auth" or not msg.get("token"):
            return None
        payload = decode_access_token(msg["token"])
        return payload
    except WebSocketDisconnect:
        # The peer already completed the close handshake. Let the handler
        # return without attempting to emit a second close frame.
        raise
    except (asyncio.TimeoutError, JWTError, json.JSONDecodeError, TypeError, RuntimeError):
        return None


async def _reauthenticate(ws: WebSocket, token: str) -> bool:
    """Refresh one user's credential; reject identity changes in-place.

    A new subject gets a fresh transport so private alerts already queued on
    the old user's writer cannot cross the authentication boundary. Returns
    False when the caller must leave its receive loop and prune the socket.
    """
    try:
        payload = decode_access_token(token)
    except JWTError:
        await _safe_send(ws, {"type": "error", "code": "auth_failed"})
        return True
    user_id = str(payload.get("sub") or "")
    if not user_id:
        await _safe_send(ws, {"type": "error", "code": "auth_failed"})
        return True
    if user_id != _ws_user.get(ws):
        await _safe_send(ws, {"type": "error", "code": "identity_changed"})
        await _safe_close(ws, code=4003, reason="Authentication identity changed")
        return False
    _ws_token_exp[ws] = float(payload.get("exp") or 0)
    await _safe_send(ws, {"type": "auth_ok"})
    return True


async def _safe_close(ws: WebSocket, *, code: int, reason: str) -> None:
    """Close a socket unless the peer won the disconnect race."""
    try:
        await ws.close(code=code, reason=reason)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def handle_market_ws(ws: WebSocket) -> None:
    await ws.accept()

    # ── Auth ─────────────────────────────────────────────────────
    try:
        payload = await _authenticate(ws)
    except WebSocketDisconnect:
        return
    if not payload:
        await _safe_close(ws, code=4001, reason="Authentication failed")
        return

    _subscriptions[ws] = set()
    _last_prices[ws] = {}

    user_id: str = payload.get("sub", "")
    _ws_user[ws] = user_id
    _user_ws.setdefault(user_id, set()).add(ws)
    _ws_token_exp[ws] = float(payload.get("exp") or 0)
    WS_CONNECTIONS.inc()

    queue: asyncio.Queue = asyncio.Queue(SEND_QUEUE_MAX)
    _send_queues[ws] = queue
    _writer_tasks[ws] = asyncio.create_task(_writer_loop(ws, queue))

    heartbeat_task = asyncio.create_task(_heartbeat(ws))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=HEARTBEAT_INTERVAL + 5)
            except asyncio.TimeoutError:
                # Client hasn't sent anything — connection is stale
                break
            except (WebSocketDisconnect, RuntimeError):
                # Starlette can surface a peer close as either exception,
                # depending on whether the close frame was observed before
                # the next receive call began.
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
                if not await _reauthenticate(ws, msg.get("token", "")):
                    break
                continue

            now_epoch = datetime.now(timezone.utc).timestamp()
            if now_epoch >= _ws_token_exp.get(ws, 0):
                await _safe_send(ws, {"type": "error", "code": "token_expired"})
                await _safe_close(ws, code=4001, reason="Token expired")
                break

            if action == "subscribe":
                symbols: list[str] = msg.get("symbols", [])
                markets: list[str] = msg.get("markets", [])
                # Build subscription keys
                new_subs: set[str] = set()
                for sym in symbols:
                    for mkt in (markets or ["US"]):
                        new_subs.add(f"{sym.upper()}:{mkt.upper()}")
                _index_replace(ws, _subscriptions.get(ws, set()), new_subs)
                _subscriptions[ws] = new_subs
                await _mirror_subs()
                # Send snapshot
                await _send_snapshot(ws, new_subs)

            elif action == "unsubscribe":
                _index_replace(ws, _subscriptions.get(ws, set()), set())
                _subscriptions[ws].clear()
                await _mirror_subs()

            elif action == "pong":
                pass  # heartbeat acknowledged

    except WebSocketDisconnect:
        pass
    finally:
        WS_CONNECTIONS.dec()
        heartbeat_task.cancel()
        _prune_socket(ws)
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
    """Serialize and send. Managed sockets (with a writer task) get the
    ordered bounded queue; unmanaged ones (pre-auth error frames, unit
    tests' bare fakes) fall back to a direct awaited send."""
    text = json.dumps(data)
    if ws in _send_queues:
        return _send_text(ws, text)
    try:
        await ws.send_text(text)
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
    dispatch_started = time.monotonic()

    # Reverse index: O(subscribers-of-symbol), not O(all connections).
    subscribers = _symbol_subs.get(sub_key)
    if subscribers:
        now_epoch = datetime.now(timezone.utc).timestamp()
        price = payload.get("price", 0)
        # One serialization per tick, however many subscribers.
        delta_text = json.dumps({"type": "delta", "symbol": payload["symbol"],
                                 "market": payload["market"], "data": payload})
        dead: list[WebSocket] = []

        for ws in list(subscribers):
            if now_epoch >= _ws_token_exp.get(ws, 0):
                # Token expired — stop pushing passive updates. The next
                # client message will close the socket cleanly.
                continue

            last = _last_prices.get(ws, {}).get(sub_key)
            if last is not None and abs(price - last) / (last or 1) < 0.0001:
                continue   # change < 0.01% — skip to reduce noise

            _last_prices.setdefault(ws, {})[sub_key] = price
            if ws in _send_queues:
                _send_text(ws, delta_text)   # never blocks the fan-out
            else:
                # Unmanaged socket (unit tests) — direct awaited send.
                try:
                    await ws.send_text(delta_text)
                except Exception:
                    dead.append(ws)

        for ws in dead:
            _prune_socket(ws)

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
        _prune_socket(ws)
