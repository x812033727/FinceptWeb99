import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from services import runtime_config_service as runtime_config

from ..schemas import RuntimeSettingIn, RuntimeSettingOut

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Runtime tunables (admin-tunable env-var overrides) ───────────

def _runtime_setting_to_schema(s: runtime_config.SettingInfo) -> RuntimeSettingOut:
    return RuntimeSettingOut(
        key=s.key,
        type=s.type,
        name=s.name,
        description=s.description,
        min_value=s.min_value,
        max_value=s.max_value,
        default_value=s.default_value,
        effective_value=s.effective_value,
        is_overridden=s.is_overridden,
        updated_at=s.updated_at,
        updated_by_email=s.updated_by_email,
    )


@router.get("/runtime-settings", response_model=list[RuntimeSettingOut])
async def list_runtime_settings(_: AdminUser, db: DB) -> list[RuntimeSettingOut]:
    """List every admin-tunable env-var override with its compiled
    default + currently effective value + audit info."""
    return [_runtime_setting_to_schema(s) for s in await runtime_config.list_settings(db)]


@router.put("/runtime-settings/{key}", response_model=RuntimeSettingOut)
async def upsert_runtime_setting(
    key: str, body: RuntimeSettingIn, user: AdminUser, db: DB,
) -> RuntimeSettingOut:
    try:
        cfg = await runtime_config.upsert(
            db, key, body.value, updated_by_id=uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _runtime_setting_to_schema(cfg)


@router.delete("/runtime-settings/{key}", status_code=204)
async def delete_runtime_setting(key: str, _: AdminUser, db: DB) -> None:
    try:
        await runtime_config.delete_override(db, key)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
