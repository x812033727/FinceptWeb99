"""
Weak-ETag middleware for a small allowlist of large, mostly-stable GETs.

Scope is deliberately narrow: screener and history responses are the
big JSON bodies that clients re-poll; a 304 skips re-transfer (nginx's
gzip already shrinks the 200 path). SSE/WS never match the allowlist,
and buffering is bounded to these endpoints only — do NOT widen the
list to anything streaming or per-user without thinking about both.

Weak validator (W/"…") because gzip at the nginx layer means the bytes
on the wire differ from the bytes hashed here; semantic equality is
exactly what a weak ETag promises.
"""
import hashlib

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

ETAG_PATH_PREFIXES: tuple[str, ...] = (
    "/api/us/screener",
    "/api/tw/screener",
    "/api/crypto/screener",
    "/api/us/history/",
    "/api/tw/history/",
    "/api/crypto/history/",
)


class ETagMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        if (
            request.method != "GET"
            or response.status_code != 200
            or not request.url.path.startswith(ETAG_PATH_PREFIXES)
        ):
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        etag = f'W/"{hashlib.md5(body).hexdigest()}"'

        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})

        headers = dict(response.headers)
        headers["ETag"] = etag
        return Response(
            content=body,
            status_code=200,
            headers=headers,
            media_type=response.media_type,
        )
