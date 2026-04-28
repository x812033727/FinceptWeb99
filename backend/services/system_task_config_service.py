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

import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.system_task_config import SystemTaskConfig
from models.user import User

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
    updated_at: datetime | None = None
    updated_by_email: str | None = None


@dataclass
class TaskTestResult:
    """Outcome of POST /admin/system-tasks/{id}/test — a one-shot ping
    that proves the chosen provider/model + the resolved API key form a
    working chain, without spending a real task call."""
    ok: bool
    provider: str
    model: str
    latency_ms: int
    sample_output: str | None = None
    error: str | None = None


def known_task_ids() -> list[str]:
    return list(_TASKS.keys())


def get_spec(task_id: str) -> SystemTaskSpec:
    spec = _TASKS.get(task_id)
    if spec is None:
        raise ValueError(f"unknown system task: {task_id!r}")
    return spec


async def list_tasks(db: AsyncSession) -> list[TaskConfig]:
    """Return every registered task with its compiled default + currently
    effective provider/model. Used by the admin UI roster.

    Includes `updated_at` + `updated_by_email` for any task that has an
    override row, so the admin UI can show "last changed by foo@bar.com,
    2h ago" inline. Outer-joining on users handles the case where the
    original setter has been deleted (ondelete=SET NULL on the FK).
    """
    stmt = (
        select(SystemTaskConfig, User.email)
        .outerjoin(User, SystemTaskConfig.updated_by_id == User.id)
    )
    rows = list((await db.execute(stmt)).all())
    by_id: dict[str, tuple[SystemTaskConfig, str | None]] = {
        r[0].task_id: (r[0], r[1]) for r in rows
    }
    out: list[TaskConfig] = []
    for tid, spec in _TASKS.items():
        entry = by_id.get(tid)
        ov, email = (entry[0], entry[1]) if entry else (None, None)
        out.append(TaskConfig(
            task_id=tid,
            name=spec.name,
            description=spec.description,
            default_provider=spec.default_provider,
            default_model=spec.default_model,
            effective_provider=ov.provider if ov else spec.default_provider,
            effective_model=ov.model if ov else spec.default_model,
            is_overridden=ov is not None,
            updated_at=ov.updated_at if ov else None,
            updated_by_email=email,
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
    # Re-fetch so server-side defaults / onupdate clauses (updated_at)
    # populate before we read them — lazy attribute loading on a detached
    # async row would trigger a greenlet error.
    await db.refresh(row)
    # Resolve the just-set updater's email by looking it up — avoids a
    # round trip to fetch the full user object when the caller only has
    # the ID. Falls back to None if the user vanished mid-flight.
    email = await db.scalar(select(User.email).where(User.id == updated_by_id))
    return TaskConfig(
        task_id=task_id,
        name=spec.name,
        description=spec.description,
        default_provider=spec.default_provider,
        default_model=spec.default_model,
        effective_provider=row.provider,
        effective_model=row.model,
        is_overridden=True,
        updated_at=row.updated_at,
        updated_by_email=email,
    )


async def test_task(
    db: AsyncSession, task_id: str,
) -> TaskTestResult:
    """One-shot ping of the task's effective LLM provider+model.

    Sends a 1-token "ping" via stream_chat to validate the resolved API
    key + provider routing actually work. Returns a `TaskTestResult` with
    latency, sample output, or an error message — never raises so the
    admin endpoint can always render a clean response.
    """
    from ai.llm_router import stream_chat   # local import to avoid cycle on cold start

    get_spec(task_id)   # validation — raises ValueError on unknown task
    provider, model = await resolve(db, task_id)

    started = time.monotonic()
    sample = ""
    error: str | None = None
    try:
        async for event in stream_chat(
            messages=[
                {"role": "system", "content": "You are a sanity-check echo bot. Reply with the single word: pong."},
                {"role": "user", "content": "ping"},
            ],
            provider=provider,
            model=model,
            max_tokens=8,
            temperature=0.0,
            db=db,
        ):
            etype = event.get("type")
            if etype == "delta":
                sample += event.get("text", "")
            elif etype == "error":
                error = str(event.get("message") or "provider error")
                break
    except Exception as exc:
        error = str(exc)
    latency_ms = int((time.monotonic() - started) * 1000)

    sample = sample.strip()[:200] or None
    if error is None and not sample:
        # Provider returned no error and no content — likely a missing API key
        # surfacing as an empty stream.
        error = "empty response (likely missing or invalid API key)"

    return TaskTestResult(
        ok=error is None,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        sample_output=sample,
        error=error,
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
