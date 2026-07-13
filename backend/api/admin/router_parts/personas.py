import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from services import persona_override_service as personas

from ..schemas import PersonaConfigOut, PersonaOverrideIn

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


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
async def list_personas(_: AdminUser, db: DB) -> list[PersonaConfigOut]:
    """List every persona with its compiled-default + currently-effective provider/model."""
    return [_persona_config_to_schema(p) for p in await personas.list_personas(db)]


@router.put("/personas/{persona_id}", response_model=PersonaConfigOut)
async def upsert_persona_override(
    persona_id: str, body: PersonaOverrideIn, user: AdminUser, db: DB,
) -> PersonaConfigOut:
    try:
        cfg = await personas.upsert_override(
            db, persona_id, body.provider, body.model, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _persona_config_to_schema(cfg)


@router.delete("/personas/{persona_id}", status_code=204)
async def delete_persona_override(persona_id: str, _: AdminUser, db: DB) -> None:
    await personas.delete_override(db, persona_id)
