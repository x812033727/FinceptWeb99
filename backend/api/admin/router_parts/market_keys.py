import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from services import market_key_service as market_keys

from ..schemas import MarketKeyInfo, MarketKeyUpsert, MarketKeyValidation

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Market-data provider keys ─────────────────────────────────────

def _market_info_to_schema(info: market_keys.KeyInfo) -> MarketKeyInfo:
    return MarketKeyInfo(
        provider=info.provider,
        has_key=info.has_key,
        source=info.source,
        masked=info.masked,
        last_validated_at=info.last_validated_at,
        last_validation_ok=info.last_validation_ok,
        last_validation_message=info.last_validation_message,
        updated_at=info.updated_at,
    )


@router.get("/market-keys", response_model=list[MarketKeyInfo])
async def list_market_keys(_: AdminUser, db: DB) -> list[MarketKeyInfo]:
    """List the current key state for every supported market-data provider.

    Mirrors /llm-keys: each row reports DB / env / none, and only the
    masked tail of the secret is ever returned.
    """
    return [_market_info_to_schema(i) for i in await market_keys.list_keys(db)]


@router.put("/market-keys/{provider}", response_model=MarketKeyInfo)
async def upsert_market_key(
    provider: str, body: MarketKeyUpsert, user: AdminUser, db: DB,
) -> MarketKeyInfo:
    if provider not in market_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    try:
        info = await market_keys.upsert_key(
            db, provider, body.api_key, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _market_info_to_schema(info)


@router.delete("/market-keys/{provider}", status_code=204)
async def delete_market_key(provider: str, _: AdminUser, db: DB) -> None:
    if provider not in market_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    await market_keys.delete_key(db, provider)


@router.post("/market-keys/{provider}/test", response_model=MarketKeyValidation)
async def test_market_key(provider: str, _: AdminUser, db: DB) -> MarketKeyValidation:
    """Resolve the active key (DB → env) and ping the provider.

    For Finnhub this hits /quote?symbol=AAPL — the same endpoint the
    connector uses, so a 200 with a non-zero current price proves both
    that the key was honoured and the network path works."""
    if provider not in market_keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    key = await market_keys.resolve_key(provider)
    if not key:
        return MarketKeyValidation(ok=False, message="no key configured")
    result = await market_keys.validate_key(provider, key)
    await market_keys.record_validation(db, provider, result)
    return MarketKeyValidation(ok=result.ok, message=result.message)
