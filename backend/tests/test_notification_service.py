"""
Unit tests for services.notification_service.

The module is the transport seam between background services (alerts,
billing) and the WebSocket layer — services call `notify_user` without
importing from api/. The WS manager registers its push function at
startup via `register_push_impl`.

Tests pin down:
  - The "no transport registered yet" path is a silent no-op (services
    that fire alerts during boot must not crash if the WS manager
    hasn't claimed the seam yet).
  - register_push_impl swaps the active implementation atomically;
    re-registering replaces the previous one (relevant for hot-reload
    in dev).
  - notify_user actually awaits the registered coroutine (a regression
    where someone changes `await self._push_impl(...)` to plain
    `self._push_impl(...)` would silently drop notifications).

Module state is reset in beforeEach via importlib.reload so each
test gets a clean module-level _push_impl.
"""
import importlib
from unittest.mock import AsyncMock

import pytest

import services.notification_service as ns


@pytest.fixture(autouse=True)
def reset_module():
    """Each test starts with the module in its post-import state
    (no transport registered)."""
    importlib.reload(ns)
    yield
    importlib.reload(ns)


# ── No transport registered ──────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_user_silently_noops_when_no_push_impl_registered():
    """Caller must not crash if WS manager hasn't claimed the seam yet
    (e.g. alert service fires during app startup, before lifespan
    handler runs register_push_impl)."""
    # Should complete without raising.
    await ns.notify_user("user-123", {"type": "alert", "msg": "hi"})


# ── Registration ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_push_impl_makes_notify_user_call_through():
    push = AsyncMock()
    ns.register_push_impl(push)

    await ns.notify_user("user-123", {"type": "alert", "msg": "BTC > 70k"})

    push.assert_awaited_once_with("user-123", {"type": "alert", "msg": "BTC > 70k"})


@pytest.mark.asyncio
async def test_register_push_impl_replaces_previous_implementation():
    """Hot-reload / test isolation use case: registering twice should
    swap the implementation atomically — the OLD one stops receiving
    calls."""
    old = AsyncMock()
    new = AsyncMock()
    ns.register_push_impl(old)
    ns.register_push_impl(new)

    await ns.notify_user("u", {"x": 1})

    new.assert_awaited_once()
    old.assert_not_awaited()


# ── Await contract ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_user_awaits_the_push_impl():
    """Regression guard: notify_user must `await` the registered
    coroutine, not just invoke it. A bare call would return a coroutine
    object that's never scheduled, silently dropping every notification.
    """
    sentinel = {"awaited": False}

    async def push(_user_id: str, _payload: dict) -> None:
        sentinel["awaited"] = True

    ns.register_push_impl(push)
    await ns.notify_user("u", {"x": 1})

    assert sentinel["awaited"] is True


@pytest.mark.asyncio
async def test_notify_user_propagates_payload_object_identity():
    """The dict passed to notify_user reaches the push impl unchanged
    (no defensive copying that would drop a future field)."""
    push = AsyncMock()
    ns.register_push_impl(push)

    payload = {"type": "alert", "id": 42, "nested": {"a": 1}}
    await ns.notify_user("u", payload)

    args, _ = push.await_args
    assert args[1] is payload  # same object, not a copy
