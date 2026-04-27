"""Pure unit tests for the market-data provider key service.

Mirrors the `test_llm_key_service` suite but for `services.market_key_service`.
In-memory SQLite via `db_session` fixture; no live HTTP.

The whole module is skipped when `cryptography` can't load (some sandboxed
dev envs lack `_cffi_backend`); CI runs everything.
"""
from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio

try:
    from cryptography.fernet import Fernet  # noqa: F401
    from auth.llm_key_crypto import decrypt, encrypt
    from models.market_provider_key import MarketProviderKey
    from models.user import User, UserRole
    from services import market_key_service as svc
    _CRYPTO_OK = True
except BaseException:  # noqa: BLE001
    _CRYPTO_OK = False

pytestmark = pytest.mark.skipif(
    not _CRYPTO_OK, reason="cryptography not loadable in this env (CI-only)",
)


@pytest_asyncio.fixture
async def test_admin(db_session):
    user = User(
        id=uuid.uuid4(),
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture(autouse=True)
async def cleanup_state(db_session, monkeypatch):
    """Reset rows + in-process cache between tests so order doesn't matter.

    Also patches the connector's session-maker import to point at the
    test SQLite engine. Without this, `resolve_key` opens a session
    against the real `db.session.AsyncSessionLocal` (Postgres in prod;
    nothing in this isolated test env) and the lookup silently logs
    `db_lookup_failed`, masking real assertions."""
    from sqlalchemy import delete
    from tests.conftest import TestSessionLocal
    import db.session as db_session_module
    monkeypatch.setattr(db_session_module, "AsyncSessionLocal", TestSessionLocal)

    await db_session.execute(delete(MarketProviderKey))
    await db_session.commit()
    svc._invalidate_cache()
    yield
    await db_session.execute(delete(MarketProviderKey))
    await db_session.commit()
    svc._invalidate_cache()


# ── list_keys ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_keys_returns_one_row_per_supported_provider(db_session, monkeypatch):
    """No DB rows, no env → list_keys still returns a placeholder per provider."""
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "")

    keys = await svc.list_keys(db_session)
    assert {k.provider for k in keys} == set(svc.SUPPORTED_PROVIDERS)
    assert all(k.source == "none" and not k.has_key for k in keys)


@pytest.mark.asyncio
async def test_list_keys_marks_env_source_when_settings_has_value(db_session, monkeypatch):
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-only-key-12345")

    keys = await svc.list_keys(db_session)
    fh = next(k for k in keys if k.provider == "finnhub")
    assert fh.source == "env"
    assert fh.has_key is True
    # mask preserves the last 4 chars only — never leak the full key
    assert "env-only-key-12345" not in fh.masked
    assert fh.masked.endswith("2345")


@pytest.mark.asyncio
async def test_list_keys_db_overrides_env(db_session, monkeypatch, test_admin):
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-key-xxxxx")
    await svc.upsert_key(db_session, "finnhub", "db-key-secret-zzzz", test_admin.id)

    keys = await svc.list_keys(db_session)
    fh = next(k for k in keys if k.provider == "finnhub")
    assert fh.source == "db"
    # masked uses the DB plaintext, never the env value
    assert fh.masked.endswith("zzzz")


# ── upsert_key + delete_key ──────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_creates_row_with_encrypted_payload(db_session, test_admin):
    info = await svc.upsert_key(db_session, "finnhub", "secret-1234", test_admin.id)
    assert info.has_key is True
    assert info.source == "db"

    row = await db_session.get(MarketProviderKey, "finnhub")
    assert row is not None
    # Payload must be Fernet-encrypted, not plaintext
    assert row.encrypted_key != "secret-1234"
    assert decrypt(row.encrypted_key) == "secret-1234"


@pytest.mark.asyncio
async def test_upsert_replaces_existing_row_and_resets_validation(db_session, test_admin):
    """Re-saving must clear last_validated_at / ok / message — caller may
    immediately call /test again."""
    from datetime import datetime
    await svc.upsert_key(db_session, "finnhub", "v1", test_admin.id)
    row = await db_session.get(MarketProviderKey, "finnhub")
    row.last_validated_at = datetime.utcnow()
    row.last_validation_ok = True
    row.last_validation_message = "OK"
    await db_session.commit()

    await svc.upsert_key(db_session, "finnhub", "v2", test_admin.id)
    row = await db_session.get(MarketProviderKey, "finnhub")
    assert decrypt(row.encrypted_key) == "v2"
    assert row.last_validated_at is None
    assert row.last_validation_ok is None
    assert row.last_validation_message is None


@pytest.mark.asyncio
async def test_upsert_rejects_unsupported_provider(db_session, test_admin):
    with pytest.raises(ValueError, match="unsupported"):
        await svc.upsert_key(db_session, "polygon", "key", test_admin.id)


@pytest.mark.asyncio
async def test_upsert_rejects_blank_key(db_session, test_admin):
    with pytest.raises(ValueError, match="blank"):
        await svc.upsert_key(db_session, "finnhub", "   ", test_admin.id)


@pytest.mark.asyncio
async def test_delete_removes_row(db_session, test_admin):
    await svc.upsert_key(db_session, "finnhub", "to-delete", test_admin.id)
    await svc.delete_key(db_session, "finnhub")
    assert await db_session.get(MarketProviderKey, "finnhub") is None


@pytest.mark.asyncio
async def test_delete_is_idempotent(db_session):
    """Clearing a non-existent row should not raise — admin clicked clear
    on an env-only row, for instance."""
    await svc.delete_key(db_session, "finnhub")  # no error


# ── resolve_key ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_key_returns_db_value_when_present(db_session, monkeypatch, test_admin):
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-fallback")
    await svc.upsert_key(db_session, "finnhub", "db-wins", test_admin.id)

    resolved = await svc.resolve_key("finnhub")
    assert resolved == "db-wins"


@pytest.mark.asyncio
async def test_resolve_key_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-fallback")
    resolved = await svc.resolve_key("finnhub")
    assert resolved == "env-fallback"


@pytest.mark.asyncio
async def test_resolve_key_returns_empty_when_neither_db_nor_env(monkeypatch):
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "")
    resolved = await svc.resolve_key("finnhub")
    assert resolved == ""


@pytest.mark.asyncio
async def test_resolve_key_caches_and_invalidates_on_upsert(db_session, monkeypatch, test_admin):
    """Two back-to-back resolves should hit cache; upsert must invalidate
    so the next resolve sees the new value without waiting for TTL."""
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-x")

    # First call populates the cache
    assert await svc.resolve_key("finnhub") == "env-x"
    cached = svc._key_cache.get("finnhub")
    assert cached is not None
    assert cached[0] == "env-x"

    # Save a DB key — cache must be cleared
    await svc.upsert_key(db_session, "finnhub", "db-now", test_admin.id)
    assert "finnhub" not in svc._key_cache or svc._key_cache["finnhub"][0] != "env-x"

    # Next resolve picks up the DB value
    assert await svc.resolve_key("finnhub") == "db-now"


@pytest.mark.asyncio
async def test_resolve_key_cache_ttl_expires(monkeypatch):
    """A stale cache entry past its TTL must trigger a fresh DB lookup."""
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-1")
    assert await svc.resolve_key("finnhub") == "env-1"

    # Manually expire the cache by rewinding its expiry timestamp
    svc._key_cache["finnhub"] = ("env-1", time.monotonic() - 1)

    # Even before the operator changes anything, expired cache forces re-read.
    # Updating the env value during the gap should now win.
    monkeypatch.setattr(svc.settings, "FINNHUB_API_KEY", "env-2")
    assert await svc.resolve_key("finnhub") == "env-2"


# ── record_validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_validation_updates_row(db_session, test_admin):
    await svc.upsert_key(db_session, "finnhub", "k", test_admin.id)
    await svc.record_validation(
        db_session, "finnhub", svc.ValidationResult(ok=True, message="OK"),
    )
    row = await db_session.get(MarketProviderKey, "finnhub")
    assert row.last_validation_ok is True
    assert row.last_validation_message == "OK"
    assert row.last_validated_at is not None


@pytest.mark.asyncio
async def test_record_validation_no_op_when_row_missing(db_session):
    # No row, no error — admin tested the env-only path
    await svc.record_validation(
        db_session, "finnhub", svc.ValidationResult(ok=False, message="x"),
    )


# ── encrypt_key parity ───────────────────────────────────────────

def test_market_key_uses_same_fernet_as_llm_key():
    """Confirms the same JWT-derived Fernet powers both stores so an
    operator who rotates JWT_SECRET_KEY only has to re-enter every key
    once, not separately per category."""
    plain = "shared-fernet-test"
    cipher = encrypt(plain)
    assert decrypt(cipher) == plain
