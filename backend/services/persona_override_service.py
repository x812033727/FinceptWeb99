"""Per-persona LLM provider/model override CRUD.

The chat endpoint reads overrides via `ai.agents.get_agent_resolved`. This
service is the admin write path + bulk-read for the admin UI list.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.agents import all_persona_ids, get_agent
from models.persona_override import PersonaOverride


@dataclass
class PersonaConfig:
    persona_id: str
    name: str
    description: str
    default_provider: str       # compiled-in
    default_model: str          # compiled-in
    effective_provider: str     # what chat will actually use
    effective_model: str
    is_overridden: bool


VALID_PROVIDERS = {
    "openai", "anthropic", "gemini", "ollama",
    "minimax", "groq", "deepseek", "openrouter",
    "claude_agent",
}


async def list_personas(db: AsyncSession) -> list[PersonaConfig]:
    rows = (await db.execute(select(PersonaOverride))).scalars().all()
    by_id = {r.persona_id: r for r in rows}
    out: list[PersonaConfig] = []
    for pid in all_persona_ids():
        spec = get_agent(pid)
        ov = by_id.get(pid)
        out.append(PersonaConfig(
            persona_id=pid,
            name=spec.name,
            description=spec.description,
            default_provider=spec.default_provider,
            default_model=spec.default_model,
            effective_provider=ov.provider if ov else spec.default_provider,
            effective_model=ov.model if ov else spec.default_model,
            is_overridden=ov is not None,
        ))
    return out


async def upsert_override(
    db: AsyncSession, persona_id: str, provider: str, model: str,
    updated_by_id: uuid.UUID,
) -> PersonaConfig:
    if persona_id not in all_persona_ids():
        raise ValueError(f"unknown persona: {persona_id}")
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"invalid provider: {provider}")
    if not model.strip():
        raise ValueError("model cannot be blank")

    row = await db.get(PersonaOverride, persona_id)
    if row is None:
        row = PersonaOverride(
            persona_id=persona_id,
            provider=provider,
            model=model.strip(),
            updated_by_id=updated_by_id,
        )
        db.add(row)
    else:
        row.provider = provider
        row.model = model.strip()
        row.updated_by_id = updated_by_id
    await db.commit()

    spec = get_agent(persona_id)
    return PersonaConfig(
        persona_id=persona_id,
        name=spec.name,
        description=spec.description,
        default_provider=spec.default_provider,
        default_model=spec.default_model,
        effective_provider=row.provider,
        effective_model=row.model,
        is_overridden=True,
    )


async def delete_override(db: AsyncSession, persona_id: str) -> None:
    row = await db.get(PersonaOverride, persona_id)
    if row is not None:
        await db.delete(row)
        await db.commit()
