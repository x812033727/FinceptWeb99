"""LLM provider key management — DB-first, .env fallback.

Public API:
  - resolve_key(provider) → str | None       (used by ai/llm_router.py)
  - list_keys(db)         → list[KeyInfo]    (admin UI list)
  - upsert_key(db, …)     → KeyInfo          (admin UI save)
  - delete_key(db, …)     → None             (admin UI clear; restores .env fallback)
  - validate_key(provider, key) → ValidationResult  (test button)

Lookup order on a chat request:
  1. DB row for the provider, decrypted (Fernet)
  2. .env-supplied settings.<PROVIDER>_API_KEY
  3. None — providers should yield "key not configured" message

Hot-path (resolve_key) is async + uses an open AsyncSession the caller passes
in. This avoids opening a fresh session per chat token.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.llm_key_crypto import decrypt, encrypt, mask
from config import settings
from models.llm_provider_key import LLMProviderKey
from models.user_llm_provider_key import UserLLMProviderKey

logger = logging.getLogger(__name__)

# Providers exposed in the admin UI. Mirrors what ai/llm_router.py knows
# how to dispatch to — ollama is omitted because it doesn't take an API key.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "gemini",
    "minimax",
    "groq",
    "deepseek",
    "openrouter",
)

# Map provider → settings attribute fallback name
_ENV_KEY_ATTR = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


@dataclass
class KeyInfo:
    provider: str
    has_key: bool
    source: Literal["db", "env", "none"]
    masked: str
    last_validated_at: datetime | None
    last_validation_ok: bool | None
    last_validation_message: str | None
    updated_at: datetime | None


@dataclass
class ValidationResult:
    ok: bool
    message: str


def _env_fallback(provider: str) -> str:
    attr = _ENV_KEY_ATTR.get(provider, "")
    return getattr(settings, attr, "") if attr else ""


async def resolve_key(
    db: AsyncSession, provider: str, user_id: uuid.UUID | None = None,
) -> str | None:
    """Return the active key for a provider.

    Lookup order:
      1. The caller's per-user row in user_llm_provider_keys (if user_id given)
      2. The system row in llm_provider_keys
      3. .env fallback
      4. (system-task path only, opt-out via SYSTEM_TASK_FALLBACK_TO_ADMIN_KEY)
         Any admin user's per-user row — solo deployments often configure
         keys only at the admin's per-user level via the UI; without this
         tier every cron silently 401s with "no key configured".

    Returns None only if every applicable tier is absent.
    """
    if user_id is not None:
        urow = await db.get(UserLLMProviderKey, (user_id, provider))
        if urow is not None:
            plain = decrypt(urow.encrypted_key)
            if plain:
                return plain
            logger.warning("llm_key.user.decrypt_failed",
                           extra={"provider": provider, "user_id": str(user_id)})

    row = await db.get(LLMProviderKey, provider)
    if row is not None:
        plain = decrypt(row.encrypted_key)
        if plain:
            return plain
        logger.warning("llm_key.system.decrypt_failed", extra={"provider": provider})

    fallback = _env_fallback(provider)
    if fallback:
        return fallback

    # Tier 4 — system task fallback to any admin user's per-user key.
    # Only triggers on the system-task path (`user_id is None`); chat
    # requests with an explicit user_id already covered tier 1.
    if user_id is None and settings.SYSTEM_TASK_FALLBACK_TO_ADMIN_KEY:
        return await _resolve_from_admin_user_key(db, provider)
    return None


async def _resolve_from_admin_user_key(
    db: AsyncSession, provider: str,
) -> str | None:
    """Find the first admin-role user that has a per-user key for
    `provider` and return the decrypted key. None if no admin has one.

    Newest-admin-first so a recently-created admin's key wins over a
    long-deactivated one. Inactive users are excluded so disabling an
    account also revokes its keys from background tasks.
    """
    from models.user import User, UserRole
    stmt = (
        select(UserLLMProviderKey, User.id)
        .join(User, User.id == UserLLMProviderKey.user_id)
        .where(
            UserLLMProviderKey.provider == provider,
            User.role == UserRole.admin,
            User.is_active.is_(True),
        )
        .order_by(User.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    for urow, _admin_id in rows:
        plain = decrypt(urow.encrypted_key)
        if plain:
            return plain
        logger.warning(
            "llm_key.admin_fallback.decrypt_failed",
            extra={"provider": provider, "user_id": str(_admin_id)},
        )
    return None


# ── Per-user key CRUD ────────────────────────────────────────────

async def list_user_keys(db: AsyncSession, user_id: uuid.UUID) -> list[KeyInfo]:
    """Per-user analogue of list_keys — system rows are NOT included."""
    rows = (await db.execute(
        select(UserLLMProviderKey).where(UserLLMProviderKey.user_id == user_id)
    )).scalars().all()
    by_provider = {r.provider: r for r in rows}

    out: list[KeyInfo] = []
    for prov in SUPPORTED_PROVIDERS:
        row = by_provider.get(prov)
        if row is None:
            out.append(KeyInfo(
                provider=prov, has_key=False, source="none", masked="",
                last_validated_at=None, last_validation_ok=None,
                last_validation_message=None, updated_at=None,
            ))
            continue
        plain = decrypt(row.encrypted_key) or ""
        out.append(KeyInfo(
            provider=prov,
            has_key=bool(plain),
            source="db" if plain else "none",
            masked=mask(plain) if plain else "(decrypt failed)",
            last_validated_at=row.last_validated_at,
            last_validation_ok=row.last_validation_ok,
            last_validation_message=row.last_validation_message,
            updated_at=row.updated_at,
        ))
    return out


async def upsert_user_key(
    db: AsyncSession, user_id: uuid.UUID, provider: str, plaintext: str,
) -> KeyInfo:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    if not plaintext.strip():
        raise ValueError("key cannot be blank")

    encrypted = encrypt(plaintext.strip())
    row = await db.get(UserLLMProviderKey, (user_id, provider))
    if row is None:
        row = UserLLMProviderKey(
            user_id=user_id, provider=provider, encrypted_key=encrypted,
        )
        db.add(row)
    else:
        row.encrypted_key = encrypted
        row.last_validated_at = None
        row.last_validation_ok = None
        row.last_validation_message = None
    await db.commit()
    await db.refresh(row)

    return KeyInfo(
        provider=provider, has_key=True, source="db",
        masked=mask(plaintext),
        last_validated_at=row.last_validated_at,
        last_validation_ok=row.last_validation_ok,
        last_validation_message=row.last_validation_message,
        updated_at=row.updated_at,
    )


async def delete_user_key(db: AsyncSession, user_id: uuid.UUID, provider: str) -> None:
    row = await db.get(UserLLMProviderKey, (user_id, provider))
    if row is not None:
        await db.delete(row)
        await db.commit()


async def record_user_validation(
    db: AsyncSession, user_id: uuid.UUID, provider: str, result: ValidationResult,
) -> None:
    row = await db.get(UserLLMProviderKey, (user_id, provider))
    if row is None:
        return
    row.last_validated_at = datetime.utcnow()
    row.last_validation_ok = result.ok
    row.last_validation_message = result.message[:500]
    await db.commit()


async def list_keys(db: AsyncSession) -> list[KeyInfo]:
    """Return one KeyInfo per supported provider (regardless of whether a row exists)."""
    rows = (await db.execute(select(LLMProviderKey))).scalars().all()
    by_provider = {r.provider: r for r in rows}

    out: list[KeyInfo] = []
    for prov in SUPPORTED_PROVIDERS:
        row = by_provider.get(prov)
        env_val = _env_fallback(prov)

        if row is not None:
            plain = decrypt(row.encrypted_key) or ""
            out.append(KeyInfo(
                provider=prov,
                has_key=bool(plain),
                source="db" if plain else "none",
                masked=mask(plain) if plain else "(decrypt failed)",
                last_validated_at=row.last_validated_at,
                last_validation_ok=row.last_validation_ok,
                last_validation_message=row.last_validation_message,
                updated_at=row.updated_at,
            ))
        elif env_val:
            out.append(KeyInfo(
                provider=prov,
                has_key=True,
                source="env",
                masked=mask(env_val),
                last_validated_at=None,
                last_validation_ok=None,
                last_validation_message=None,
                updated_at=None,
            ))
        else:
            out.append(KeyInfo(
                provider=prov,
                has_key=False,
                source="none",
                masked="",
                last_validated_at=None,
                last_validation_ok=None,
                last_validation_message=None,
                updated_at=None,
            ))
    return out


async def upsert_key(
    db: AsyncSession, provider: str, plaintext: str, updated_by_id: uuid.UUID,
) -> KeyInfo:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    if not plaintext.strip():
        raise ValueError("key cannot be blank")

    encrypted = encrypt(plaintext.strip())
    row = await db.get(LLMProviderKey, provider)
    if row is None:
        row = LLMProviderKey(
            provider=provider,
            encrypted_key=encrypted,
            updated_by_id=updated_by_id,
        )
        db.add(row)
    else:
        row.encrypted_key = encrypted
        row.updated_by_id = updated_by_id
        # Reset validation status — caller may immediately call validate_key.
        row.last_validated_at = None
        row.last_validation_ok = None
        row.last_validation_message = None
    await db.commit()
    await db.refresh(row)

    return KeyInfo(
        provider=provider,
        has_key=True,
        source="db",
        masked=mask(plaintext),
        last_validated_at=row.last_validated_at,
        last_validation_ok=row.last_validation_ok,
        last_validation_message=row.last_validation_message,
        updated_at=row.updated_at,
    )


async def delete_key(db: AsyncSession, provider: str) -> None:
    row = await db.get(LLMProviderKey, provider)
    if row is not None:
        await db.delete(row)
        await db.commit()


async def validate_key(provider: str, key: str) -> ValidationResult:
    """Make a tiny live call to the provider to verify the key works.

    Cost: 1-2 tokens at most. If the provider exposes a free model-list
    endpoint we use that instead.
    """
    if not key.strip():
        return ValidationResult(False, "key is empty")

    try:
        if provider == "openai":
            return await _validate_openai(key)
        if provider == "anthropic":
            return await _validate_anthropic(key)
        if provider == "gemini":
            return await _validate_gemini(key)
        if provider == "minimax":
            return await _validate_minimax(key)
        if provider == "groq":
            return await _validate_openai_compat(
                key, settings.GROQ_HOST, label="groq",
            )
        if provider == "deepseek":
            return await _validate_openai_compat(
                key, settings.DEEPSEEK_HOST, label="deepseek",
            )
        if provider == "openrouter":
            return await _validate_openai_compat(
                key, settings.OPENROUTER_HOST, label="openrouter",
            )
        return ValidationResult(False, f"validation not implemented for {provider}")
    except Exception as exc:
        logger.warning("llm_key.validate_failed",
                       extra={"provider": provider, "error": str(exc)})
        return ValidationResult(False, f"validation error: {exc}")


async def record_validation(
    db: AsyncSession, provider: str, result: ValidationResult,
) -> None:
    row = await db.get(LLMProviderKey, provider)
    if row is None:
        return
    row.last_validated_at = datetime.utcnow()
    row.last_validation_ok = result.ok
    row.last_validation_message = result.message[:500]
    await db.commit()


# ── Per-provider validators (cheap pings) ────────────────────────

_VALIDATE_TIMEOUT = 10.0


async def _validate_openai(key: str) -> ValidationResult:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
    if r.status_code == 200:
        return ValidationResult(True, "OK")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")


async def _validate_anthropic(key: str) -> ValidationResult:
    # Anthropic has no free-list endpoint — use a 1-token messages call.
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            content=json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }),
        )
    if r.status_code == 200:
        return ValidationResult(True, "OK")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")


async def _validate_gemini(key: str) -> ValidationResult:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
        )
    if r.status_code == 200:
        return ValidationResult(True, "OK")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")


async def _validate_minimax(key: str) -> ValidationResult:
    host = settings.MINIMAX_HOST.rstrip("/")
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.post(
            f"{host}/v1/text/chatcompletion_v2",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            content=json.dumps({
                "model": settings.MINIMAX_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }),
        )
    if r.status_code == 200:
        return ValidationResult(True, "OK")
    return ValidationResult(False, f"HTTP {r.status_code}: {r.text[:200]}")


async def _validate_openai_compat(
    key: str, host: str, *, label: str,
) -> ValidationResult:
    """Validate any OpenAI-compatible provider via GET /v1/models.

    Groq, DeepSeek and OpenRouter all expose this endpoint with the same
    Bearer-token contract as OpenAI, so a 200 response with a non-empty
    `data` list confirms the key works without spending tokens.
    """
    base = host.rstrip("/")
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as client:
        r = await client.get(
            f"{base}/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
    if r.status_code == 200:
        return ValidationResult(True, "OK")
    return ValidationResult(False, f"{label} HTTP {r.status_code}: {r.text[:200]}")
