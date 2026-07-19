"""Secret-safe text helpers for FinMind diagnostics and telemetry.

Upstream HTTP exceptions may include the complete request URL. FinMind's
generic endpoint authenticates with a query-string token, so persisting or
logging an exception verbatim can disclose credentials. Keep redaction in one
place and apply it both before storage and before operator-facing output.
"""
from __future__ import annotations

import re
from typing import Any

_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|token|secret|password)\s*=\s*)"
    r"(?P<value>[^&\s'\"\\]+)"
)
_JSON_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|token|secret|password)[\"']?\s*:\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^,\s\"'\}\]]+)"
)
_BEARER_RE = re.compile(
    r"(?i)(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9._~+/=-]+)"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
_URL_PASSWORD_RE = re.compile(
    r"(?P<prefix>://[^:/@\s]+:)(?P<value>[^@\s/]+)(?P<suffix>@)"
)

REDACTED = "<redacted>"


def redact_secret_text(value: Any | None) -> str | None:
    """Return printable text with common credential shapes removed.

    This deliberately accepts any object so exception and database values can
    use the same safe boundary. Redaction happens before callers truncate the
    result; otherwise a truncated token could evade pattern matching.
    """
    if value is None:
        return None

    text = str(value)
    text = _URL_PASSWORD_RE.sub(
        rf"\g<prefix>{REDACTED}\g<suffix>", text
    )
    text = _QUERY_SECRET_RE.sub(rf"\g<prefix>{REDACTED}", text)
    text = _JSON_SECRET_RE.sub(rf"\g<prefix>{REDACTED}", text)
    text = _BEARER_RE.sub(rf"\g<prefix>{REDACTED}", text)
    return _JWT_RE.sub(REDACTED, text)


def redact_exception(exc: BaseException) -> str:
    """Render an exception without exposing credentials in its message."""
    safe = redact_secret_text(repr(exc))
    return safe or exc.__class__.__name__
