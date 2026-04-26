"""Kraken WebSocket v2 ticker pump.

Maintains a single outbound WS connection to wss://ws.kraken.com/v2,
subscribes to all Top 20 ticker channels, and forwards each price update
to a callback (the chat endpoint binds this to publish_update so existing
client WS subscribers see live prices).

Lifecycle:
- Started in main.py's lifespan
- Reconnects on disconnect with exponential backoff (1s → 2s → 4s → ... cap 60s)
- run() never raises; logs and retries
- stop() sets a flag the run loop checks between reconnects

Why not just use polling forever?
The 30s scheduler poll is fine for portfolio summaries, but for a stock
detail page where the user is staring at a chart, sub-second updates feel
materially better and Kraken's free WS removes the rate-limit concern of
polling more aggressively.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from data.crypto.symbols import TOP20, to_kraken_pair, from_kraken_response_key

logger = logging.getLogger(__name__)

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"

# Backoff schedule for reconnects (seconds). Cap at 60 to avoid completely
# losing the connection if Kraken is temporarily unstable.
_BACKOFF_SCHEDULE = (1, 2, 4, 8, 16, 30, 60)

# Callback signature: async (canonical_symbol, market='CRYPTO', payload_dict) -> None
# Matches api/websocket/manager.publish_update so the pump can be bound directly.
TickerCallback = Callable[[str, str, dict], Awaitable[None]]


class KrakenTickerPump:
    """Maintains a Kraken WS subscription, forwards ticks to a callback."""

    def __init__(self, callback: TickerCallback):
        self._callback = callback
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self.run(), name="kraken-ticker-pump")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def run(self) -> None:
        """Outer reconnect loop. Returns only when stop() is called."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._connect_and_pump()
                attempt = 0  # successful pump exit (e.g. clean close) — reset
            except Exception as exc:  # noqa: BLE001 — pump must never die unhandled
                wait = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
                attempt += 1
                logger.warning("kraken_ws.disconnect", extra={
                    "error": str(exc), "retry_in_s": wait, "attempt": attempt,
                })
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass

    async def _connect_and_pump(self) -> None:
        """Single connection lifecycle: connect, subscribe, dispatch ticks."""
        async with websockets.connect(KRAKEN_WS_URL, ping_interval=20, ping_timeout=10) as ws:
            await self._subscribe_all(ws)
            logger.info("kraken_ws.connected", extra={"pairs": len(TOP20)})

            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                except ConnectionClosed:
                    raise

                await self._handle_message(raw)

    async def _subscribe_all(self, ws) -> None:
        """Subscribe to ticker channel for every Top 20 pair Kraken supports."""
        pairs = [to_kraken_pair(s) for s in TOP20]
        pairs = [p for p in pairs if p is not None]
        # Kraken v2 expects the spot pair format with a slash, e.g. "BTC/USD".
        # We translate XBTUSD → XBT/USD here; Kraken accepts both forms in the
        # response but the v2 subscribe message wants the slashed version.
        v2_pairs = [_to_v2_pair(p) for p in pairs]
        await ws.send(json.dumps({
            "method": "subscribe",
            "params": {"channel": "ticker", "symbol": v2_pairs},
        }))

    async def _handle_message(self, raw: str) -> None:
        """Parse one WS frame and forward ticker updates to the callback."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Kraken v2 update shape:
        # {"channel": "ticker", "type": "update"|"snapshot",
        #  "data": [{"symbol": "BTC/USD", "last": 100200.5, "change_pct": 1.5, ...}]}
        if msg.get("channel") != "ticker":
            return
        for tick in msg.get("data") or []:
            v2_symbol = tick.get("symbol", "")
            canonical = _from_v2_pair(v2_symbol)
            if canonical is None:
                continue
            try:
                await self._callback(canonical, "CRYPTO", {
                    "price": float(tick.get("last", 0) or 0),
                    "change_pct": float(tick.get("change_pct", 0) or 0),
                    "volume": float(tick.get("volume", 0) or 0),
                })
            except Exception as exc:  # noqa: BLE001 — one bad tick mustn't kill the pump
                logger.debug("kraken_ws.callback_failed: %s", exc)


def _to_v2_pair(rest_pair: str) -> str:
    """REST format `XBTUSD` → v2 format `XBT/USD`. Assume USD quote."""
    if rest_pair.endswith("USD"):
        return f"{rest_pair[:-3]}/USD"
    return rest_pair


def _from_v2_pair(v2_pair: str) -> str | None:
    """v2 format `BTC/USD` or `XBT/USD` → canonical symbol (BTC).

    Reuses the existing REST response-key map by stripping the slash.
    """
    if "/" not in v2_pair:
        return None
    base = v2_pair.split("/", 1)[0]
    # Try the REST-key map first (handles XBT → BTC, XDG → DOGE).
    canonical = from_kraken_response_key(f"{base}USD")
    if canonical:
        return canonical
    # Otherwise assume the base is already canonical and within our universe.
    if base in TOP20:
        return base
    return None
