"""Pure unit tests for the LLM provider key service.

In-memory SQLite via the existing TestSessionLocal fixture; no live HTTP.

Note: this suite imports `cryptography.fernet`, which may fail to load in
some sandboxed dev environments due to a missing `_cffi_backend` compiled
extension. We skip the whole module gracefully in that case so the rest of
the test run isn't blocked. CI (full Python install) executes everything.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

try:
    from cryptography.fernet import Fernet  # noqa: F401
    from auth.llm_key_crypto import _fernet, decrypt, encrypt, mask
    from models.llm_provider_key import LLMProviderKey
    from models.user import User, UserRole
    from services import llm_key_service as keys
    _CRYPTO_OK = True
except BaseException:  # noqa: BLE001 — pyo3 panics aren't Exception subclasses
    _CRYPTO_OK = False

pytestmark = pytest.mark.skipif(
    not _CRYPTO_OK, reason="cryptography not loadable in this env (CI-only)",
)


@pytest_asyncio.fixture
async def test_user(db_session):
    user = User(
        id=uuid.uuid4(),
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return {"id": str(user.id), "role": "admin"}


@pytest_asyncio.fixture(autouse=True)
async def cleanup_keys(db_session):
    """Each test starts with no llm_provider_keys rows."""
    from sqlalchemy import delete
    await db_session.execute(delete(LLMProviderKey))
    await db_session.commit()
    yield
    await db_session.execute(delete(LLMProviderKey))
    await db_session.commit()


# ── Encryption round-trip ────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    plain = "sk-test-secret-123"
    cipher = encrypt(plain)
    assert cipher != plain
    assert decrypt(cipher) == plain


def test_encrypt_rejects_empty():
    with pytest.raises(ValueError):
        encrypt("")


def test_decrypt_returns_none_on_garbage():
    assert decrypt("not-a-fernet-token") is None
    assert decrypt("") is None


def test_decrypt_returns_none_when_signed_by_different_key():
    other = Fernet(Fernet.generate_key())
    foreign = other.encrypt(b"hi").decode()
    assert decrypt(foreign) is None  # different secret → caller falls back to env


def test_fernet_is_cached():
    # _fernet uses lru_cache(1); same instance on repeat calls.
    assert _fernet() is _fernet()


def test_mask_short_keys():
    assert mask("") == ""
    assert mask("ab") == "••"
    assert mask("abcd") == "••••"
    assert mask("abcde") == "•" * 8 + "bcde"
    assert mask("sk-very-long-key-1234") == "•" * 8 + "1234"


# ── Service round-trip with real DB ─────────────────────────────

@pytest.mark.asyncio
async def test_resolve_key_falls_back_to_env(db_session, monkeypatch):
    monkeypatch.setattr(keys.settings, "OPENAI_API_KEY", "from-env-12345")
    val = await keys.resolve_key(db_session, "openai")
    assert val == "from-env-12345"


@pytest.mark.asyncio
async def test_resolve_key_returns_none_when_no_db_no_env(db_session, monkeypatch):
    monkeypatch.setattr(keys.settings, "OPENAI_API_KEY", "")
    val = await keys.resolve_key(db_session, "openai")
    assert val is None


@pytest.mark.asyncio
async def test_upsert_then_resolve_uses_db(db_session, test_user, monkeypatch):
    monkeypatch.setattr(keys.settings, "ANTHROPIC_API_KEY", "should-be-overridden")
    info = await keys.upsert_key(db_session, "anthropic", "sk-ant-real-key", uuid.UUID(test_user["id"]))
    assert info.has_key is True
    assert info.source == "db"
    assert info.masked.endswith("-key")

    resolved = await keys.resolve_key(db_session, "anthropic")
    assert resolved == "sk-ant-real-key"


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_row(db_session, test_user):
    uid = uuid.UUID(test_user["id"])
    await keys.upsert_key(db_session, "openai", "first-key", uid)
    info = await keys.upsert_key(db_session, "openai", "second-key", uid)
    assert (await keys.resolve_key(db_session, "openai")) == "second-key"
    # Validation status reset on update.
    assert info.last_validated_at is None
    assert info.last_validation_ok is None


@pytest.mark.asyncio
async def test_delete_key_restores_env_fallback(db_session, test_user, monkeypatch):
    uid = uuid.UUID(test_user["id"])
    monkeypatch.setattr(keys.settings, "GEMINI_API_KEY", "env-gemini-key")
    await keys.upsert_key(db_session, "gemini", "db-gemini-key", uid)
    assert (await keys.resolve_key(db_session, "gemini")) == "db-gemini-key"

    await keys.delete_key(db_session, "gemini")
    assert (await keys.resolve_key(db_session, "gemini")) == "env-gemini-key"


@pytest.mark.asyncio
async def test_upsert_rejects_unsupported_provider(db_session, test_user):
    uid = uuid.UUID(test_user["id"])
    with pytest.raises(ValueError, match="unsupported provider"):
        await keys.upsert_key(db_session, "fictional_provider", "x", uid)


@pytest.mark.asyncio
async def test_upsert_rejects_blank_key(db_session, test_user):
    uid = uuid.UUID(test_user["id"])
    with pytest.raises(ValueError, match="blank"):
        await keys.upsert_key(db_session, "openai", "  ", uid)


@pytest.mark.asyncio
async def test_list_keys_reports_all_supported_providers(db_session, monkeypatch):
    # Make exactly one source available per provider so we cover all branches.
    monkeypatch.setattr(keys.settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(keys.settings, "ANTHROPIC_API_KEY", "anth-env")
    monkeypatch.setattr(keys.settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(keys.settings, "MINIMAX_API_KEY", "")

    # openai → DB row; minimax → still empty.
    db_session.add(LLMProviderKey(provider="openai", encrypted_key=encrypt("oai-db-key")))
    await db_session.commit()

    rows = await keys.list_keys(db_session)
    by_provider = {r.provider: r for r in rows}

    assert set(by_provider.keys()) == set(keys.SUPPORTED_PROVIDERS)
    assert by_provider["openai"].source == "db"
    assert by_provider["openai"].masked.endswith("-key")
    assert by_provider["anthropic"].source == "env"
    assert by_provider["anthropic"].has_key is True
    assert by_provider["gemini"].source == "none"
    assert by_provider["gemini"].has_key is False
    assert by_provider["minimax"].source == "none"


@pytest.mark.asyncio
async def test_resolve_key_survives_decrypt_failure(db_session, monkeypatch):
    """When the row's ciphertext can't be decrypted (e.g. JWT_SECRET_KEY rotated),
    resolve_key must NOT crash — it should fall back to the .env value."""
    # Insert a row with garbage ciphertext that can't be decoded.
    db_session.add(LLMProviderKey(provider="openai", encrypted_key="not-a-valid-token"))
    await db_session.commit()
    monkeypatch.setattr(keys.settings, "OPENAI_API_KEY", "fallback-from-env")

    val = await keys.resolve_key(db_session, "openai")
    assert val == "fallback-from-env"


# ── System-task fallback to admin user's per-user key (PR #206) ────

@pytest.mark.asyncio
async def test_system_task_falls_back_to_admin_user_key(
    db_session, monkeypatch,
):
    """Solo deployments configure keys at the admin user's per-user
    level via the UI. Without the tier-4 fallback every cron silently
    401s ('no key configured') even though the user can chat fine in
    AIPage."""
    monkeypatch.setattr(keys.settings, "ANTHROPIC_API_KEY", "")
    admin = User(
        id=uuid.uuid4(),
        email=f"admin-fallback-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    await keys.upsert_user_key(db_session, admin.id, "anthropic", "sk-ant-admin-key")

    # System-task path (user_id=None) — finds admin's key.
    val = await keys.resolve_key(db_session, "anthropic", user_id=None)
    assert val == "sk-ant-admin-key"


@pytest.mark.asyncio
async def test_admin_fallback_skips_inactive_admin(db_session, monkeypatch):
    """Disabling an admin account also revokes its keys from background
    tasks — defence against a fired-admin's key being silently used by
    cron forever."""
    monkeypatch.setattr(keys.settings, "OPENAI_API_KEY", "")
    inactive_admin = User(
        id=uuid.uuid4(),
        email=f"ex-admin-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.admin,
        is_active=False,
    )
    db_session.add(inactive_admin)
    await db_session.commit()
    await keys.upsert_user_key(db_session, inactive_admin.id, "openai", "sk-leaked")

    val = await keys.resolve_key(db_session, "openai", user_id=None)
    assert val is None


@pytest.mark.asyncio
async def test_admin_fallback_skips_non_admin_user(db_session, monkeypatch):
    """Only admin-role users participate in the fallback. A
    viewer/analyst's key must not be reachable from a cron task."""
    monkeypatch.setattr(keys.settings, "GEMINI_API_KEY", "")
    viewer = User(
        id=uuid.uuid4(),
        email=f"viewer-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.viewer,
        is_active=True,
    )
    db_session.add(viewer)
    await db_session.commit()
    await keys.upsert_user_key(db_session, viewer.id, "gemini", "viewer-key")

    val = await keys.resolve_key(db_session, "gemini", user_id=None)
    assert val is None


@pytest.mark.asyncio
async def test_admin_fallback_can_be_disabled_via_setting(
    db_session, monkeypatch,
):
    """Multi-tenant deployments may want admin-key isolation from
    background workloads. `SYSTEM_TASK_FALLBACK_TO_ADMIN_KEY=False`
    restores the pre-PR-206 behaviour (system row + .env only)."""
    monkeypatch.setattr(keys.settings, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(keys.settings, "SYSTEM_TASK_FALLBACK_TO_ADMIN_KEY", False)
    admin = User(
        id=uuid.uuid4(),
        email=f"strict-admin-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.admin,
        is_active=True,
    )
    db_session.add(admin)
    await db_session.commit()
    await keys.upsert_user_key(db_session, admin.id, "anthropic", "sk-ant-key")

    val = await keys.resolve_key(db_session, "anthropic", user_id=None)
    assert val is None


@pytest.mark.asyncio
async def test_user_path_does_not_trigger_admin_fallback(
    db_session, monkeypatch,
):
    """When the caller passes its own `user_id` (chat path) and that
    user has no key, we must NOT silently borrow the admin's key.
    Each user owns their own quota / billing context."""
    monkeypatch.setattr(keys.settings, "OPENAI_API_KEY", "")
    admin = User(
        id=uuid.uuid4(),
        email=f"admin-isolated-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.admin,
        is_active=True,
    )
    other_user = User(
        id=uuid.uuid4(),
        email=f"viewer-isolated-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        role=UserRole.viewer,
        is_active=True,
    )
    db_session.add_all([admin, other_user])
    await db_session.commit()
    await keys.upsert_user_key(db_session, admin.id, "openai", "sk-admin-only")

    # Calling with the viewer's user_id must NOT return the admin's key.
    val = await keys.resolve_key(db_session, "openai", user_id=other_user.id)
    assert val is None
