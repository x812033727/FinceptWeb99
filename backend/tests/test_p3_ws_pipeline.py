"""P3 WS pipeline: reverse index, send queues, serialize-once dispatch."""
import asyncio
import json
import time
from unittest.mock import patch

import pytest

import api.websocket.manager as mgr


class FakeWS:
    def __init__(self, delay: float = 0.0):
        self.sent: list[dict] = []
        self.delay = delay

    async def send_text(self, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(json.loads(text))


@pytest.fixture(autouse=True)
def reset_state():
    dicts = (mgr._subscriptions, mgr._last_prices, mgr._ws_user, mgr._user_ws,
             mgr._ws_token_exp, mgr._symbol_subs, mgr._send_queues,
             mgr._writer_tasks)
    for d in dicts:
        d.clear()
    yield
    for d in dicts:
        d.clear()


def _far_future() -> float:
    return time.time() + 3600


def _register(ws, keys: set[str]) -> None:
    mgr._index_replace(ws, mgr._subscriptions.get(ws, set()), keys)
    mgr._subscriptions[ws] = keys
    mgr._last_prices[ws] = {}
    mgr._ws_token_exp[ws] = _far_future()


# ── reverse index ─────────────────────────────────────────────────

def test_index_replace_moves_membership():
    ws = FakeWS()
    _register(ws, {"AAPL:US", "2330:TW"})
    assert mgr._symbol_subs == {"AAPL:US": {ws}, "2330:TW": {ws}}

    _register(ws, {"2330:TW", "BTC:CRYPTO"})
    assert "AAPL:US" not in mgr._symbol_subs        # empty sets are removed
    assert mgr._symbol_subs["BTC:CRYPTO"] == {ws}
    assert mgr._symbol_subs["2330:TW"] == {ws}


def test_prune_socket_clears_every_registry():
    ws = FakeWS()
    _register(ws, {"AAPL:US"})
    mgr._ws_user[ws] = "u1"
    mgr._user_ws["u1"] = {ws}
    mgr._send_queues[ws] = asyncio.Queue(4)

    mgr._prune_socket(ws)

    assert ws not in mgr._subscriptions
    assert "AAPL:US" not in mgr._symbol_subs
    assert ws not in mgr._send_queues
    assert "u1" not in mgr._user_ws


# ── serialize-once ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_serializes_payload_once_for_many_subscribers():
    sockets = [FakeWS() for _ in range(5)]
    for ws in sockets:
        _register(ws, {"AAPL:US"})

    calls = []
    real_dumps = json.dumps

    def counting_dumps(obj, *a, **kw):
        calls.append(obj)
        return real_dumps(obj, *a, **kw)

    with patch.object(mgr.json, "dumps", side_effect=counting_dumps):
        await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 1.0})

    delta_serializations = [c for c in calls
                            if isinstance(c, dict) and c.get("type") == "delta"]
    assert len(delta_serializations) == 1           # once, not per-subscriber
    assert all(len(ws.sent) == 1 for ws in sockets)


# ── send queue ────────────────────────────────────────────────────

def test_send_text_drops_oldest_when_full():
    ws = FakeWS()
    q: asyncio.Queue = asyncio.Queue(2)
    mgr._send_queues[ws] = q

    assert mgr._send_text(ws, "m1")
    assert mgr._send_text(ws, "m2")
    assert mgr._send_text(ws, "m3")                 # full → m1 dropped

    assert q.qsize() == 2
    assert q.get_nowait() == "m2"
    assert q.get_nowait() == "m3"


@pytest.mark.asyncio
async def test_writer_delivers_in_order_and_slow_client_isolated():
    fast, slow = FakeWS(), FakeWS(delay=0.05)
    for ws in (fast, slow):
        _register(ws, {"AAPL:US"})
        q: asyncio.Queue = asyncio.Queue(mgr.SEND_QUEUE_MAX)
        mgr._send_queues[ws] = q
        mgr._writer_tasks[ws] = asyncio.create_task(mgr._writer_loop(ws, q))

    started = time.monotonic()
    for price in (1.0, 2.0, 3.0):
        await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": price})
    dispatch_elapsed = time.monotonic() - started

    # Fan-out never awaited the slow client's socket.
    assert dispatch_elapsed < 0.04

    await asyncio.sleep(0.01)
    assert [m["data"]["price"] for m in fast.sent] == [1.0, 2.0, 3.0]

    await asyncio.sleep(0.2)                        # let the slow writer drain
    assert [m["data"]["price"] for m in slow.sent] == [1.0, 2.0, 3.0]

    for ws in (fast, slow):
        mgr._prune_socket(ws)


@pytest.mark.asyncio
async def test_writer_send_failure_prunes_socket():
    class DeadWS(FakeWS):
        async def send_text(self, text: str) -> None:
            raise ConnectionError("gone")

    ws = DeadWS()
    _register(ws, {"AAPL:US"})
    q: asyncio.Queue = asyncio.Queue(4)
    mgr._send_queues[ws] = q
    task = asyncio.create_task(mgr._writer_loop(ws, q))
    mgr._writer_tasks[ws] = task

    mgr._send_text(ws, json.dumps({"type": "delta"}))
    await asyncio.wait_for(task, timeout=1.0)       # writer exits on failure

    assert ws not in mgr._send_queues
    assert "AAPL:US" not in mgr._symbol_subs
