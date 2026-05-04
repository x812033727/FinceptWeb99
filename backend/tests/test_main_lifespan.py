"""Tests for `main._auto_init_finmind_clone`.

The lifespan startup hook tries to bring the FinMind clone DB up to
head + seeded so the operator's only manual setup step is starting
the postgres_finmind container. Tests pin that contract:

  - FINMIND_AUTO_INIT=false → skip silently (operator opt-out)
  - DB unreachable           → log warning + proceed (don't crash boot)
  - DB reachable             → migrate + seed catalog + seed free plan
"""
from __future__ import annotations

import logging
import os

# In-memory SQLite for the FinMind subsystem. Must be set before any
# test in this file imports main (which imports finmind.config).
os.environ.setdefault(
    "FINMIND_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
)

import pytest  # noqa: E402

# `main` is import-heavy (loads every router + the rate limiter +
# slowapi), and importing it at module load time breaks the
# conftest's autouse `mock_redis` fixture for tests in OTHER files
# that share the test session — the imports happen before pytest
# applies the patch, so a real redis connection lingers. Late-import
# the helper inside each test instead.


class _CaptureHandler(logging.Handler):
    """In-memory handler that survives `setup_logging()`'s
    `root.handlers = [handler]` overwrite. We attach it directly to
    the `main` logger (not the root) so the JSON formatter on root
    doesn't matter — records flow up via propagation, then ours
    captures locally."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def main_log() -> _CaptureHandler:
    """Per-test handler attached to `logging.getLogger("main")`.
    Yields the handler so the test can inspect `.records`."""
    handler = _CaptureHandler()
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger("main")
    prior_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


@pytest.mark.asyncio
async def test_auto_init_skips_when_disabled(monkeypatch, main_log):
    """Operator opted out via env var → no migration / no seed.

    `setup_logging()` replaces the root logger's handler list at
    process start, which detaches pytest's caplog handler. Use
    capsys against the JSON-formatted stdout instead — that's what
    operators actually read in production logs anyway."""
    from main import _auto_init_finmind_clone
    from finmind.config import finmind_settings

    monkeypatch.setattr(finmind_settings, "FINMIND_AUTO_INIT", False)

    # If any of these were called we'd see an exception (no real DB
    # session bound). The skip path must NOT touch them.
    called: dict[str, bool] = {"migrate": False, "seed": False}

    def _migrate_marker():
        called["migrate"] = True

    async def _seed_marker():
        called["seed"] = True
        return (0, 0)

    monkeypatch.setattr(
        "finmind.scripts.init_db.run_migrations", _migrate_marker,
    )
    monkeypatch.setattr(
        "finmind.scripts.init_db.seed_dataset_sources", _seed_marker,
    )

    await _auto_init_finmind_clone()

    assert called["migrate"] is False
    assert called["seed"] is False
    msgs = [r.getMessage() for r in main_log.records]
    assert any("auto-init disabled" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_auto_init_skips_when_db_unreachable(monkeypatch, main_log):
    """ConnectionRefusedError on the SELECT 1 probe → warn + proceed.
    The migrate / seed steps must NOT run (would error on an
    unreachable engine anyway, but the explicit skip is what keeps
    the warning message clean instead of stack-tracing)."""
    from main import _auto_init_finmind_clone
    from finmind.config import finmind_settings

    monkeypatch.setattr(finmind_settings, "FINMIND_AUTO_INIT", True)

    class _FailingConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, *a, **kw):
            raise ConnectionRefusedError("[Errno 111] Connection refused")

    class _FailingEngine:
        def connect(self):
            return _FailingConn()

    called: dict[str, bool] = {"migrate": False}

    def _migrate_marker():
        called["migrate"] = True

    monkeypatch.setattr(
        "finmind.db.session.finmind_engine", _FailingEngine(),
    )
    monkeypatch.setattr(
        "finmind.scripts.init_db.run_migrations", _migrate_marker,
    )

    await _auto_init_finmind_clone()  # must not raise

    assert called["migrate"] is False
    msgs = [r.getMessage() for r in main_log.records]
    assert any(
        "DB not reachable" in m and "ConnectionRefusedError" in m
        for m in msgs
    ), msgs


@pytest.mark.asyncio
async def test_auto_init_runs_migrate_and_seeds_when_db_reachable(
    monkeypatch, main_log,
):
    """Happy path: probe succeeds, both migrate + both seed functions
    are called, success is logged with the seeded count."""
    from main import _auto_init_finmind_clone
    from finmind.config import finmind_settings

    monkeypatch.setattr(finmind_settings, "FINMIND_AUTO_INIT", True)

    class _OkConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, *a, **kw):
            return None

    class _OkEngine:
        def connect(self):
            return _OkConn()

    called: dict[str, int] = {
        "migrate": 0, "seed_catalog": 0, "seed_free_plan": 0,
    }

    def _migrate():
        called["migrate"] += 1

    async def _seed_catalog():
        called["seed_catalog"] += 1
        return (80, 80)

    async def _seed_free_plan():
        called["seed_free_plan"] += 1
        return True

    monkeypatch.setattr(
        "finmind.db.session.finmind_engine", _OkEngine(),
    )
    monkeypatch.setattr(
        "finmind.scripts.init_db.run_migrations", _migrate,
    )
    monkeypatch.setattr(
        "finmind.scripts.init_db.seed_dataset_sources", _seed_catalog,
    )
    monkeypatch.setattr(
        "finmind.scripts.init_db.seed_default_free_plan", _seed_free_plan,
    )

    await _auto_init_finmind_clone()

    assert called["migrate"] == 1
    assert called["seed_catalog"] == 1
    assert called["seed_free_plan"] == 1
    msgs = [r.getMessage() for r in main_log.records]
    assert any("auto-init done" in m and "80" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_auto_init_logs_and_swallows_seed_failure(monkeypatch, main_log):
    """If the migrate or seed step raises mid-flight (e.g. catalog
    UPSERT fails on a corrupted table), the error is logged with
    full traceback but lifespan continues — main app must still boot."""
    from main import _auto_init_finmind_clone
    from finmind.config import finmind_settings

    monkeypatch.setattr(finmind_settings, "FINMIND_AUTO_INIT", True)

    class _OkConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, *a, **kw):
            return None

    class _OkEngine:
        def connect(self):
            return _OkConn()

    def _migrate_explodes():
        raise RuntimeError("alembic crash")

    monkeypatch.setattr(
        "finmind.db.session.finmind_engine", _OkEngine(),
    )
    monkeypatch.setattr(
        "finmind.scripts.init_db.run_migrations", _migrate_explodes,
    )

    await _auto_init_finmind_clone()  # must not raise

    msgs = [r.getMessage() for r in main_log.records]
    assert any("auto-init failed" in m for m in msgs), msgs
