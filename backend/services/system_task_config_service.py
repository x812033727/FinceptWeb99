"""LLM provider/model overrides for background system tasks.

Each registered task has a compiled-in default (`SystemTaskSpec`) plus an
optional admin override stored in `system_task_configs`. Callers use
`resolve(task_id)` to get the effective `(provider, model)` pair without
caring whether an override exists.

Adding a new task:
  1. Register a `SystemTaskSpec` in `_TASKS` below.
  2. In the task code, call `await resolve(db, "<task_id>")` to get the
     effective provider/model — never hard-code.
  3. The admin UI surfaces the new entry automatically via
     `list_tasks()`.

The provider whitelist is intentionally a copy of `persona_override_service.
VALID_PROVIDERS` rather than imported — keeps the two services
independently evolvable without a circular dep risk.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.system_task_config import SystemTaskConfig

VALID_PROVIDERS = {
    "openai", "anthropic", "gemini", "ollama",
    "minimax", "groq", "deepseek", "openrouter",
}


@dataclass(frozen=True)
class SystemTaskSpec:
    task_id: str
    name: str
    description: str
    default_provider: str
    default_model: str


# Registry of all task IDs the admin UI will surface. Any task that wants
# admin-routable LLM selection must register here.
_TASKS: dict[str, SystemTaskSpec] = {
    "news_sentiment": SystemTaskSpec(
        task_id="news_sentiment",
        name="新聞情緒打分",
        description=(
            "每 30 分鐘對未打分的新聞批次評分（bullish / bearish / neutral）"
            "供討論室引用。便宜模型即可勝任。"
        ),
        default_provider="anthropic",
        default_model="claude-haiku-4-5-20251001",
    ),
    "discussion_synthesizer": SystemTaskSpec(
        task_id="discussion_synthesizer",
        name="討論結論彙整",
        description=(
            "讀完整場圓桌發言後，輸出結構化結論 JSON："
            "推薦標的 / 共識度 / 風險 / 時間框架。需要長文閱讀+結構化輸出。"
        ),
        default_provider="anthropic",
        default_model="claude-haiku-4-5-20251001",
    ),
}


@dataclass
class TaskConfig:
    task_id: str
    name: str
    description: str
    default_provider: str
    default_model: str
    effective_provider: str
    effective_model: str
    is_overridden: bool


def known_task_ids() -> list[str]:
    return list(_TASKS.keys())


def get_spec(task_id: str) -> SystemTaskSpec:
    spec = _TASKS.get(task_id)
    if spec is None:
        raise ValueError(f"unknown system task: {task_id!r}")
    return spec


async def list_tasks(db: AsyncSession) -> list[TaskConfig]:
    """Return every registered task with its compiled default + currently
    effective provider/model. Used by the admin UI roster."""
    rows = (await db.execute(select(SystemTaskConfig))).scalars().all()
    by_id = {r.task_id: r for r in rows}
    out: list[TaskConfig] = []
    for tid, spec in _TASKS.items():
        ov = by_id.get(tid)
        out.append(TaskConfig(
            task_id=tid,
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
    db: AsyncSession,
    task_id: str,
    provider: str,
    model: str,
    updated_by_id: uuid.UUID,
) -> TaskConfig:
    spec = get_spec(task_id)
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"invalid provider: {provider}")
    if not model.strip():
        raise ValueError("model cannot be blank")

    row = await db.get(SystemTaskConfig, task_id)
    if row is None:
        row = SystemTaskConfig(
            task_id=task_id,
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
    return TaskConfig(
        task_id=task_id,
        name=spec.name,
        description=spec.description,
        default_provider=spec.default_provider,
        default_model=spec.default_model,
        effective_provider=row.provider,
        effective_model=row.model,
        is_overridden=True,
    )


async def delete_override(db: AsyncSession, task_id: str) -> None:
    row = await db.get(SystemTaskConfig, task_id)
    if row is not None:
        await db.delete(row)
        await db.commit()


async def resolve(db: AsyncSession, task_id: str) -> tuple[str, str]:
    """Return (provider, model) for the task — admin override if present,
    otherwise the compiled-in default. Falls back to the default on any
    DB error so a transient outage doesn't halt the background task.

    Raises ValueError if the task ID isn't registered (programming error,
    not a runtime condition).
    """
    spec = get_spec(task_id)
    try:
        row = await db.get(SystemTaskConfig, task_id)
    except Exception:
        return spec.default_provider, spec.default_model
    if row is None:
        return spec.default_provider, spec.default_model
    return row.provider, row.model
