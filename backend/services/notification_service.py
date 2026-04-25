"""User notification dispatch.

Decouples services that need to push something to a user (alerts, future
billing/limit notices) from the WebSocket transport. The WebSocket manager
registers its `push_alert_to_user` implementation at startup; services call
`notify_user(...)` without importing from `api/`.
"""
from typing import Awaitable, Callable

PushFn = Callable[[str, dict], Awaitable[None]]

_push_impl: PushFn | None = None


def register_push_impl(fn: PushFn) -> None:
    global _push_impl
    _push_impl = fn


async def notify_user(user_id: str, payload: dict) -> None:
    """Best-effort push. Silently no-ops if no transport is registered."""
    if _push_impl is None:
        return
    await _push_impl(user_id, payload)
