"""
Unit tests for the WebSocket manager's pure logic:
  - JWT authentication (valid token, timeout, malformed payload)
  - Delta suppression (< 0.01% change skipped)
  - Dead-socket pruning after send failure
  - alert local delivery fan-out to multi-tab user connections
  - Subscription state cleanup on disconnect

These tests exercise the manager module directly without a running
FastAPI app or real WebSocket, so they sidestep the pyo3/cffi issue
that blocks the integration-test client fixture locally.
"""
import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from api.websocket import manager as mgr


def _far_future() -> float:
    """Helper: a token expiry well past every test's runtime so the
    manager's freshness check doesn't drop legitimately-connected sockets."""
    return time.time() + 3600


# ── Test double ───────────────────────────────────────────────────

class FakeWS:
    """Minimal WebSocket stand-in: captures sent payloads, can be made to fail."""

    def __init__(self, fail_on_send: bool = False):
        self.sent: list[dict] = []
        self.fail_on_send = fail_on_send

    async def send_text(self, text: str) -> None:
        if self.fail_on_send:
            raise ConnectionError("socket closed")
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        raise NotImplementedError("subclasses should override")


def _register(ws, keys: set[str]) -> None:
    """Register a fake socket the way the handler does: forward map AND
    the reverse index _dispatch fans out through."""
    mgr._index_replace(ws, mgr._subscriptions.get(ws, set()), keys)
    mgr._subscriptions[ws] = keys


@pytest.fixture(autouse=True)
def reset_manager_state():
    """Each test starts with clean module-level dicts."""
    for d in (mgr._subscriptions, mgr._last_prices, mgr._ws_user,
              mgr._user_ws, mgr._ws_token_exp, mgr._symbol_subs,
              mgr._send_queues, mgr._writer_tasks):
        d.clear()
    yield
    for d in (mgr._subscriptions, mgr._last_prices, mgr._ws_user,
              mgr._user_ws, mgr._ws_token_exp, mgr._symbol_subs,
              mgr._send_queues, mgr._writer_tasks):
        d.clear()


# ── _authenticate ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_authenticate_accepts_valid_jwt():
    ws = MagicMock()
    ws.receive_text = AsyncMock(
        return_value=json.dumps({"action": "auth", "token": "stub-token"})
    )
    fake_payload = {"sub": "user-abc", "role": "analyst"}

    with patch(
        "api.websocket.manager.decode_access_token", return_value=fake_payload,
    ) as mock_decode:
        payload = await mgr._authenticate(ws)

    mock_decode.assert_called_once_with("stub-token")
    assert payload == fake_payload


@pytest.mark.asyncio
async def test_authenticate_rejects_missing_action():
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value=json.dumps({"token": "anything"}))

    assert await mgr._authenticate(ws) is None


@pytest.mark.asyncio
async def test_authenticate_rejects_missing_token():
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value=json.dumps({"action": "auth"}))

    assert await mgr._authenticate(ws) is None


@pytest.mark.asyncio
async def test_authenticate_rejects_invalid_jwt():
    ws = MagicMock()
    ws.receive_text = AsyncMock(
        return_value=json.dumps({"action": "auth", "token": "not.a.valid.jwt"})
    )

    assert await mgr._authenticate(ws) is None


@pytest.mark.asyncio
async def test_authenticate_rejects_malformed_json():
    ws = MagicMock()
    ws.receive_text = AsyncMock(return_value="{not json")

    assert await mgr._authenticate(ws) is None


@pytest.mark.asyncio
async def test_authenticate_times_out():
    """If the client doesn't send within AUTH_TIMEOUT, authenticate returns None."""

    async def never_returns():
        await asyncio.sleep(10)
        return ""

    ws = MagicMock()
    ws.receive_text = never_returns

    # Shrink the timeout to keep the test snappy
    original = mgr.AUTH_TIMEOUT
    mgr.AUTH_TIMEOUT = 0.05
    try:
        result = await mgr._authenticate(ws)
    finally:
        mgr.AUTH_TIMEOUT = original

    assert result is None


# ── _dispatch: delta suppression ─────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_sends_delta_to_subscriber():
    ws = FakeWS()
    _register(ws, {"AAPL:US"})
    mgr._last_prices[ws] = {}
    mgr._ws_token_exp[ws] = _far_future()

    await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 180.0})

    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "delta"
    assert ws.sent[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_dispatch_skips_non_subscribers():
    ws = FakeWS()
    _register(ws, {"MSFT:US"})
    mgr._last_prices[ws] = {}
    mgr._ws_token_exp[ws] = _far_future()

    await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 180.0})

    assert ws.sent == []


@pytest.mark.asyncio
async def test_dispatch_suppresses_tiny_change():
    """Changes below 0.01% should be skipped to reduce WS chatter."""
    ws = FakeWS()
    _register(ws, {"AAPL:US"})
    mgr._last_prices[ws] = {"AAPL:US": 180.0}
    mgr._ws_token_exp[ws] = _far_future()

    # +0.001% change — below threshold
    await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 180.0018})
    assert ws.sent == []


@pytest.mark.asyncio
async def test_dispatch_sends_on_meaningful_change():
    ws = FakeWS()
    _register(ws, {"AAPL:US"})
    mgr._last_prices[ws] = {"AAPL:US": 180.0}
    mgr._ws_token_exp[ws] = _far_future()

    # +0.5% change — well above threshold
    await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 180.90})
    assert len(ws.sent) == 1
    assert mgr._last_prices[ws]["AAPL:US"] == pytest.approx(180.90)


@pytest.mark.asyncio
async def test_dispatch_prunes_dead_sockets():
    """A send failure should evict the socket from _subscriptions/_last_prices."""
    ws_ok = FakeWS()
    ws_dead = FakeWS(fail_on_send=True)
    _register(ws_ok, {"AAPL:US"})
    _register(ws_dead, {"AAPL:US"})
    mgr._last_prices[ws_ok] = {}
    mgr._last_prices[ws_dead] = {}
    mgr._ws_token_exp[ws_ok] = _far_future()
    mgr._ws_token_exp[ws_dead] = _far_future()

    await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 180.0})

    assert ws_ok in mgr._subscriptions
    assert ws_dead not in mgr._subscriptions
    assert ws_dead not in mgr._last_prices


@pytest.mark.asyncio
async def test_dispatch_skips_expired_token_subscriber():
    """Sockets whose access token has expired should stop receiving deltas
    even if their subscription is still registered."""
    ws_fresh = FakeWS()
    ws_expired = FakeWS()
    _register(ws_fresh, {"AAPL:US"})
    _register(ws_expired, {"AAPL:US"})
    mgr._last_prices[ws_fresh] = {}
    mgr._last_prices[ws_expired] = {}
    mgr._ws_token_exp[ws_fresh] = _far_future()
    mgr._ws_token_exp[ws_expired] = time.time() - 1  # expired a second ago

    await mgr._dispatch({"symbol": "AAPL", "market": "US", "price": 180.0})

    assert len(ws_fresh.sent) == 1
    assert ws_expired.sent == []


# ── _deliver_alert_local (was push_alert_to_user) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_push_alert_fans_out_to_all_user_tabs():
    tab1, tab2 = FakeWS(), FakeWS()
    mgr._user_ws["user-1"] = {tab1, tab2}
    mgr._ws_user[tab1] = "user-1"
    mgr._ws_user[tab2] = "user-1"
    mgr._ws_token_exp[tab1] = _far_future()
    mgr._ws_token_exp[tab2] = _far_future()

    await mgr._deliver_alert_local("user-1", {"type": "alert", "symbol": "AAPL"})

    assert len(tab1.sent) == 1
    assert len(tab2.sent) == 1
    assert tab1.sent[0]["type"] == "alert"


@pytest.mark.asyncio
async def test_push_alert_skips_expired_token_tabs():
    """A tab whose access token has expired should not receive alert pushes."""
    tab_fresh, tab_expired = FakeWS(), FakeWS()
    mgr._user_ws["user-1"] = {tab_fresh, tab_expired}
    mgr._ws_user[tab_fresh] = "user-1"
    mgr._ws_user[tab_expired] = "user-1"
    mgr._ws_token_exp[tab_fresh] = _far_future()
    mgr._ws_token_exp[tab_expired] = time.time() - 1

    await mgr._deliver_alert_local("user-1", {"type": "alert"})

    assert len(tab_fresh.sent) == 1
    assert tab_expired.sent == []


@pytest.mark.asyncio
async def test_push_alert_ignores_unknown_user():
    tab = FakeWS()
    mgr._user_ws["user-1"] = {tab}

    await mgr._deliver_alert_local("user-unknown", {"type": "alert"})
    assert tab.sent == []


@pytest.mark.asyncio
async def test_push_alert_prunes_dead_sockets_and_cleans_user_index():
    tab_ok = FakeWS()
    tab_dead = FakeWS(fail_on_send=True)
    mgr._user_ws["user-1"] = {tab_ok, tab_dead}
    mgr._ws_user[tab_ok] = "user-1"
    mgr._ws_user[tab_dead] = "user-1"
    mgr._subscriptions[tab_dead] = {"AAPL:US"}
    mgr._last_prices[tab_dead] = {"AAPL:US": 180.0}
    mgr._ws_token_exp[tab_ok] = _far_future()
    mgr._ws_token_exp[tab_dead] = _far_future()

    await mgr._deliver_alert_local("user-1", {"type": "alert"})

    assert tab_dead not in mgr._user_ws["user-1"]
    assert tab_dead not in mgr._ws_user
    assert tab_dead not in mgr._subscriptions
    assert tab_ok in mgr._user_ws["user-1"]


@pytest.mark.asyncio
async def test_push_alert_deletes_empty_user_bucket():
    """Last tab closing for a user should remove the user_id key entirely."""
    only_tab = FakeWS(fail_on_send=True)
    mgr._user_ws["user-1"] = {only_tab}
    mgr._ws_user[only_tab] = "user-1"
    mgr._ws_token_exp[only_tab] = _far_future()

    await mgr._deliver_alert_local("user-1", {"type": "alert"})

    assert "user-1" not in mgr._user_ws
