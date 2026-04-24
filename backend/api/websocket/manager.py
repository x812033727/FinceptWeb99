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
from datetime import datetime, timezone
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect
from jose import JWTError

from auth.jwt_handler import decode_access_token
from cache.redis_cache import get_redis

# symbol+market → last sent price (for delta detection)
type PriceCache = dict[str, float]

# websocket → set of "SYMBOL:MARKET" strings
_subscriptions: dict[WebSocket, set[str]] = {}
# websocket → last-price cache
_last_prices: dict[WebSocket, PriceCache] = {}

_listener_task: asyncio.Task | None = None
AUTH_TIMEOUT = 5.0       # seconds to send auth message after connect
HEARTBEAT_INTERVAL = 30  # seconds


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

            if action == "subscribe":
                symbols: list[str] = msg.get("symbols", [])
                markets: list[str] = msg.get("markets", [])
                # Build subscription keys
                new_subs: set[str] = set()
                for sym in symbols:
                    for mkt in (markets or ["US"]):
                        new_subs.add(f"{sym.upper()}:{mkt.upper()}")
                _subscriptions[ws] = new_subs
                # Send snapshot
                await _send_snapshot(ws, new_subs)

            elif action == "unsubscribe":
                _subscriptions[ws].clear()

            elif action == "pong":
                pass  # heartbeat acknowledged

    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        _subscriptions.pop(ws, None)
        _last_prices.pop(ws, None)


async def _send_snapshot(ws: WebSocket, subs: set[str]) -> None:
    """Fetch latest cached quote for each subscription and send as snapshot."""
    from cache.redis_cache import cache_get, key_quote

    snapshot: dict = {}
    for sub_key in subs:
        parts = sub_key.split(":", 1)
        if len(parts) != 2:
            continue
        symbol, market = parts
        cached = await cache_get(key_quote(market.lower(), symbol))
        if cached:
            snapshot[sub_key] = json.loads(cached)

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


async def start_pubsub_listener() -> None:
    """
    Runs as a long-lived background task.
    Subscribes to Redis channel and dispatches updates to connected WebSockets.
    """
    global _listener_task
    _listener_task = asyncio.create_task(_listen_loop())


async def _listen_loop() -> None:
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(PUBSUB_CHANNEL)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                payload: dict = json.loads(message["data"])
                await _dispatch(payload)
            except Exception:
                continue
    finally:
        await pubsub.unsubscribe(PUBSUB_CHANNEL)


async def _dispatch(payload: dict) -> None:
    """
    payload = {"symbol": "AAPL", "market": "US", "price": 182.5, "change_pct": 0.4, "ts": ...}
    Send delta to every WebSocket subscribed to this symbol:market.
    Only send if price changed by more than 0.01%.
    """
    sub_key = f"{payload['symbol']}:{payload['market']}"
    dead: list[WebSocket] = []

    for ws, subs in list(_subscriptions.items()):
        if sub_key not in subs:
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


async def publish_update(symbol: str, market: str, data: dict) -> None:
    """Called by polling tasks to push a price update into the Pub/Sub bus."""
    r = await get_redis()
    payload = {"symbol": symbol, "market": market, **data}
    await r.publish(PUBSUB_CHANNEL, json.dumps(payload))
