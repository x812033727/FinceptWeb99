"""
Structured JSON logging for production.
In development (DEBUG=true) falls back to standard text format.
"""
import logging
import sys

from config import settings


def setup_logging() -> None:
    level = logging.DEBUG if settings.DEBUG else logging.INFO

    if settings.DEBUG:
        fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        logging.basicConfig(level=level, format=fmt, stream=sys.stdout)
    else:
        try:
            from pythonjsonlogger import jsonlogger

            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%SZ",
                    rename_fields={"levelname": "level", "asctime": "ts"},
                )
            )
            root = logging.getLogger()
            root.setLevel(level)
            root.handlers = [handler]
        except ImportError:
            # Fall back gracefully if python-json-logger isn't installed
            logging.basicConfig(level=level, stream=sys.stdout)

    # Silence chatty third-party loggers
    for noisy in ("uvicorn.access", "httpx", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
