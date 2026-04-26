import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from models.alert import PriceAlert
from models.user import User, UserRole
from models.watchlist import Watchlist
from api.system.router import VersionStatus
from services import llm_key_service as keys
from services import persona_override_service as personas
from services import llm_usage_service as usage
from services.version_service import force_refresh_status, trigger_update
from .schemas import (
    ActiveUpdate,
    AdminUserItem,
    LLMKeyInfo,
    LLMKeyUpsert,
    LLMKeyValidation,
    PersonaConfigOut,
    PersonaOverrideIn,
    RoleUpdate,
    SystemStats,
    UpdateResult,
    UsageBucketOut,
    UsageDayPoint,
    UsageSummaryOut,
)

router = APIRouter()
Admin = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]

VALID_ROLES = {r.value for r in UserRole}


@router.get("/stats", response_model=SystemStats)
async def stats(_: Admin, db: DB):
    total_users = await db.scalar(select(func.count(User.id)))
    active_users = await db.scalar(
        select(func.count(User.id)).where(User.is_active.is_(True))
    )
    by_role_rows = await db.execute(
        select(User.role, func.count(User.id)).group_by(User.role)
    )
    users_by_role = {row[0].value: row[1] for row in by_role_rows}

    total_alerts = await db.scalar(select(func.count(PriceAlert.id)))
    total_watchlists = await db.scalar(select(func.count(Watchlist.id)))

    return SystemStats(
        total_users=total_users or 0,
        active_users=active_users or 0,
        users_by_role=users_by_role,
        total_alerts=total_alerts or 0,
        total_watchlists=total_watchlists or 0,
    )


@router.get("/users", response_model=list[AdminUserItem])
async def list_users(
    _: Admin,
    db: DB,
    offset: int = 0,
    limit: int = 50,
):
    rows = await db.scalars(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    return list(rows.all())


@router.patch("/users/{user_id}/role", status_code=204)
async def update_role(user_id: uuid.UUID, body: RoleUpdate, _: Admin, db: DB):
    if body.role not in VALID_ROLES:
        raise HTTPException(400, f"Invalid role. Must be one of: {VALID_ROLES}")
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.role = UserRole(body.role)
    await db.commit()


@router.patch("/users/{user_id}/active", status_code=204)
async def update_active(user_id: uuid.UUID, body: ActiveUpdate, _: Admin, db: DB):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = body.is_active
    await db.commit()


@router.post("/update", response_model=UpdateResult)
async def trigger_system_update(_: Admin) -> UpdateResult:
    result = await trigger_update()
    return UpdateResult(**result)


@router.post("/version/check", response_model=VersionStatus)
async def check_for_updates(_: Admin) -> VersionStatus:
    """Force a fresh GitHub lookup, bypassing the Redis cache.

    Same payload shape as `GET /api/system/version` so the frontend can drop
    the response straight into its `["version"]` query cache.
    """
    data = await force_refresh_status()
    return VersionStatus(**data)


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
async def list_llm_keys(_: Admin, db: DB) -> list[LLMKeyInfo]:
    """List the current key state for every supported LLM provider.

    Each row reports whether a DB-stored key exists, an .env fallback is in
    use, or no key is set; the actual secret never leaves the server — only
    the masked tail is returned.
    """
    return [_info_to_schema(i) for i in await keys.list_keys(db)]


@router.put("/llm-keys/{provider}", response_model=LLMKeyInfo)
async def upsert_llm_key(
    provider: str, body: LLMKeyUpsert, user: Admin, db: DB,
) -> LLMKeyInfo:
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    try:
        info = await keys.upsert_key(db, provider, body.api_key, uuid.UUID(user["id"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _info_to_schema(info)


@router.delete("/llm-keys/{provider}", status_code=204)
async def delete_llm_key(provider: str, _: Admin, db: DB) -> None:
    if provider not in keys.SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"unsupported provider: {provider}")
    await keys.delete_key(db, provider)


# ── Per-persona model routing ────────────────────────────────────

def _persona_config_to_schema(p: personas.PersonaConfig) -> PersonaConfigOut:
    return PersonaConfigOut(
        persona_id=p.persona_id,
        name=p.name,
        description=p.description,
        default_provider=p.default_provider,
        default_model=p.default_model,
        effective_provider=p.effective_provider,
        effective_model=p.effective_model,
        is_overridden=p.is_overridden,
    )


@router.get("/personas", response_model=list[PersonaConfigOut])
async def list_personas(_: Admin, db: DB) -> list[PersonaConfigOut]:
    """List every persona with its compiled-default + currently-effective provider/model."""
    return [_persona_config_to_schema(p) for p in await personas.list_personas(db)]


@router.put("/personas/{persona_id}", response_model=PersonaConfigOut)
async def upsert_persona_override(
    persona_id: str, body: PersonaOverrideIn, user: Admin, db: DB,
) -> PersonaConfigOut:
    try:
        cfg = await personas.upsert_override(
            db, persona_id, body.provider, body.model, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _persona_config_to_schema(cfg)


@router.delete("/personas/{persona_id}", status_code=204)
async def delete_persona_override(persona_id: str, _: Admin, db: DB) -> None:
    await personas.delete_override(db, persona_id)


# ── LLM usage summary (admin-wide) ───────────────────────────────

def _summary_to_schema(s: usage.UsageSummary) -> UsageSummaryOut:
    return UsageSummaryOut(
        range_days=s.range_days,
        user_scoped=s.user_scoped,
        total_requests=s.total_requests,
        total_prompt_tokens=s.total_prompt_tokens,
        total_completion_tokens=s.total_completion_tokens,
        total_cost_usd=s.total_cost_usd,
        by_provider=[UsageBucketOut(**b.__dict__) for b in s.by_provider],
        by_day=[UsageDayPoint(**d) for d in s.by_day],
    )


@router.get("/llm-usage", response_model=UsageSummaryOut)
async def admin_llm_usage(
    _: Admin, db: DB, range_days: int = 30,
) -> UsageSummaryOut:
    """System-wide LLM usage aggregate for the last `range_days` days."""
    range_days = max(1, min(range_days, 365))
    summary = await usage.usage_summary(db, range_days=range_days)
    return _summary_to_schema(summary)


@router.post("/llm-keys/{provider}/test", response_model=LLMKeyValidation)
async def test_llm_key(provider: str, _: Admin, db: DB) -> LLMKeyValidation:
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
