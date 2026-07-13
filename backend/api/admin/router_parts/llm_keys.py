import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from services import llm_key_service as keys

from ..schemas import LLMKeyInfo, LLMKeyUpsert, LLMKeyValidation

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── LLM provider keys ─────────────────────────────────────────────

def _info_to_schema(info: keys.KeyInfo) -> LLMKeyInfo:
    return LLMKeyInfo(
        provider=info.provider,
        has_key=info.has_key,
        source=info.source,
        masked=info.masked,
        last_validated_at=info.last_validated_at,
        last_validation_ok=info.last_validation_ok,
        last_validation_message=info.last_validation_message,
        updated_at=info.updated_at,
    )


@router.get("/llm-keys", response_model=list[LLMKeyInfo])
async def list_llm_keys(_: AdminUser, db: DB) -> list[LLMKeyInfo]:
    """List the current key state for every supported LLM provider.

    Each row reports whether a DB-stored key exists, an .env fallback is in
    use, or no key is set; the actual secret never leaves the server — only
    the masked tail is returned.
    """
    return [_info_to_schema(i) for i in await keys.list_keys(db)]


@router.put("/llm-keys/{provider}", response_model=LLMKeyInfo)
async def upsert_llm_key(
    provider: str, body: LLMKeyUpsert, user: AdminUser, db: DB,
) -> LLMKeyInfo:
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    try:
        info = await keys.upsert_key(db, provider, body.api_key, uuid.UUID(user["id"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _info_to_schema(info)


@router.delete("/llm-keys/{provider}", status_code=204)
async def delete_llm_key(provider: str, _: AdminUser, db: DB) -> None:
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    await keys.delete_key(db, provider)


@router.post("/llm-keys/{provider}/test", response_model=LLMKeyValidation)
async def test_llm_key(provider: str, _: AdminUser, db: DB) -> LLMKeyValidation:
    """Make a tiny live call to the provider to verify the saved key works.

    Resolves the active key (DB → .env fallback) and returns ok/false plus
    the provider's error message if any. Persists the result onto the row
    so the UI can show a 'last validated' badge.
    """
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    key = await keys.resolve_key(db, provider)
    if not key:
        return LLMKeyValidation(ok=False, message="no key configured")
    result = await keys.validate_key(provider, key)
    await keys.record_validation(db, provider, result)
    return LLMKeyValidation(ok=result.ok, message=result.message)
