import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.permissions import require_admin
from db.session import get_db
from services import system_task_config_service as system_tasks

from ..schemas import (
    SystemTaskConfigOut,
    SystemTaskOverrideIn,
    SystemTaskTestResult,
)

router = APIRouter()
AdminUser = Annotated[dict, Depends(require_admin)]
DB = Annotated[AsyncSession, Depends(get_db)]


# ── Per-task LLM routing (background system tasks) ───────────────

def _system_task_to_schema(t: system_tasks.TaskConfig) -> SystemTaskConfigOut:
    return SystemTaskConfigOut(
        task_id=t.task_id,
        name=t.name,
        description=t.description,
        default_provider=t.default_provider,
        default_model=t.default_model,
        effective_provider=t.effective_provider,
        effective_model=t.effective_model,
        is_overridden=t.is_overridden,
        updated_at=t.updated_at,
        updated_by_email=t.updated_by_email,
    )


@router.get("/system-tasks", response_model=list[SystemTaskConfigOut])
async def list_system_tasks(_: AdminUser, db: DB) -> list[SystemTaskConfigOut]:
    """List every background task that supports admin LLM routing, with
    its compiled default and currently effective provider/model."""
    return [_system_task_to_schema(t) for t in await system_tasks.list_tasks(db)]


@router.put("/system-tasks/{task_id}", response_model=SystemTaskConfigOut)
async def upsert_system_task_override(
    task_id: str, body: SystemTaskOverrideIn, user: AdminUser, db: DB,
) -> SystemTaskConfigOut:
    try:
        cfg = await system_tasks.upsert_override(
            db, task_id, body.provider, body.model, uuid.UUID(user["id"]),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _system_task_to_schema(cfg)


@router.delete("/system-tasks/{task_id}", status_code=204)
async def delete_system_task_override(task_id: str, _: AdminUser, db: DB) -> None:
    try:
        await system_tasks.delete_override(db, task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post(
    "/system-tasks/{task_id}/test", response_model=SystemTaskTestResult,
)
async def test_system_task(
    task_id: str, _: AdminUser, db: DB,
) -> SystemTaskTestResult:
    """Smoke-test the task's resolved provider/model with a 1-token ping.

    Uses whatever override is currently saved (or the compiled default if
    none). Surface this in the admin UI so operators can verify a fresh
    API key + model combo works before relying on the next scheduled run.
    """
    try:
        result = await system_tasks.test_task(db, task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return SystemTaskTestResult(**result.__dict__)
