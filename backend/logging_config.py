"""
Structured JSON logging for production.
In development (DEBUG=true) falls back to standard text format.
"""
import logging
import re
import sys

from config import settings

_REDACT_PLACEHOLDER = "***"

# Match either "key": "value" (json) or key=value (extra dict / f-string) for any
# field name that looks sensitive. Case-insensitive. Catches common framings the
# app emits (jose JWTs, bcrypt hashes, OpenAI keys, refresh tokens, etc).
_SENSITIVE_KEY = (
    r"(?:password|passwd|secret|api[_-]?key|token|access[_-]?token|"
    r"refresh[_-]?token|jwt|authorization|set-cookie|bearer|email)"
)
_REDACT_PATTERNS = [
    re.compile(rf'("{_SENSITIVE_KEY}"\s*:\s*)"[^"]*"', re.IGNORECASE),
    re.compile(rf"(\b{_SENSITIVE_KEY}\s*=\s*)([^\s,'\"&]+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
]


def _redact(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    for pat in _REDACT_PATTERNS[:2]:
        text = pat.sub(lambda m: f'{m.group(1)}"{_REDACT_PLACEHOLDER}"' if m.group(0).endswith('"') else f"{m.group(1)}{_REDACT_PLACEHOLDER}", text)
    text = _REDACT_PATTERNS[2].sub(lambda m: f"{m.group(1)}{_REDACT_PLACEHOLDER}", text)
    return text


class RedactingFilter(logging.Filter):
    """Strip secrets from log records before they hit any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            try:
                if isinstance(record.args, dict):
                    record.args = {k: _redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
            except Exception:
                pass
        for key, val in list(record.__dict__.items()):
            if isinstance(val, str) and key not in {"name", "pathname", "filename", "module", "funcName", "msg", "exc_text"}:
                record.__dict__[key] = _redact(val)
        return True


def setup_logging() -> None:
    level = logging.DEBUG if settings.DEBUG else logging.INFO
    redact_filter = RedactingFilter()

    if settings.DEBUG:
        fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt))
        handler.addFilter(redact_filter)
        root = logging.getLogger()
        root.setLevel(level)
        root.handlers = [handler]
    else:
        handler = logging.StreamHandler(sys.stdout)
        try:
            from pythonjsonlogger import jsonlogger

            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%SZ",
                    rename_fields={"levelname": "level", "asctime": "ts"},
                )
            )
        except ImportError:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(redact_filter)
        root = logging.getLogger()
        root.setLevel(level)
        root.handlers = [handler]

    # Silence chatty third-party loggers
    for noisy in ("uvicorn.access", "httpx", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
