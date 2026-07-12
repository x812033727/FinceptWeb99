"""User notification dispatch.

Decouples services that need to push something to a user (alerts, future
billing/limit notices) from the delivery transports. Transports register
by name at startup and `notify_user(...)` fans out to all of them;
services never import from `api/`.

Transports (PR-D3 grew the single seam into a named registry):
  - "websocket": the WS manager's `publish_alert_to_user`, registered in
    EVERY process via the legacy `register_push_impl` alias.
  - "web_push":  `services.web_push_service.push_to_user` — browser Web
    Push so alerts land even with no tab open.

Fan-out is best-effort and isolated per transport: one transport
raising (e.g. the push service is down) must never stop the others or
crash the alert-firing cron, so exceptions are logged and swallowed.
"""
import logging
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

PushFn = Callable[[str, dict], Awaitable[None]]

_transports: dict[str, PushFn] = {}


def register_transport(name: str, fn: PushFn) -> None:
    """Register (or replace) a named delivery transport."""
    _transports[name] = fn


def register_push_impl(fn: PushFn) -> None:
    """Legacy alias: register the WebSocket transport. Kept so existing
    callers (main.py / worker.py lifespans, tests) keep working."""
    register_transport("websocket", fn)


async def notify_user(user_id: str, payload: dict) -> None:
    """Best-effort fan-out to every registered transport. Silently
    no-ops if no transport is registered."""
    for name, fn in list(_transports.items()):
        try:
            await fn(user_id, payload)
        except Exception:
            log.exception("notification transport %r failed for user %s",
                          name, user_id)
