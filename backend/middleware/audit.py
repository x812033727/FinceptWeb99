"""Append-only audit trail for every state-changing API request."""
import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from auth.jwt_handler import decode_access_token
from models.governance import AuditEvent

log = logging.getLogger(__name__)
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.method not in _WRITE_METHODS or not request.url.path.startswith("/api/"):
            return response

        actor_id = getattr(request.state, "audit_user_id", None)
        if actor_id is None:
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Bearer "):
                try:
                    actor_id = decode_access_token(authorization[7:]).get("sub")
                except Exception:
                    actor_id = None

        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        path_parts = [part for part in request.url.path.split("/") if part]
        resource_type = path_parts[1] if len(path_parts) > 1 else "api"
        resource_id = next(iter(request.path_params.values()), None)
        try:
            # Resolve dynamically so worker/test session-factory swaps are
            # honoured instead of freezing the import-time binding.
            from db import session as db_session
            async with db_session.AsyncSessionLocal() as db:
                db.add(AuditEvent(
                    actor_user_id=uuid.UUID(actor_id) if actor_id else None,
                    action=f"{request.method} {route_path}",
                    resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id is not None else None,
                    outcome="success" if response.status_code < 400 else "failure",
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    event_metadata={"status_code": response.status_code},
                ))
                await db.commit()
            response.headers["X-Audit-Recorded"] = "1"
        except Exception:
            # Audit storage degradation must be visible, but must not mutate
            # an already-produced business response.
            log.exception("audit event persistence failed", extra={"action": route_path})
        return response
