import uuid
from datetime import datetime
from pydantic import BaseModel


class AdminUserItem(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleUpdate(BaseModel):
    role: str  # "viewer" | "analyst" | "admin"


class ActiveUpdate(BaseModel):
    is_active: bool


class SystemStats(BaseModel):
    total_users: int
    active_users: int
    users_by_role: dict[str, int]
    total_alerts: int
    total_watchlists: int


class UpdateResult(BaseModel):
    status: str  # "started" | "not_configured" | "failed"
    message: str


# ── LLM provider keys ─────────────────────────────────────────────

class LLMKeyInfo(BaseModel):
    """One row per provider in the admin UI list."""
    provider: str
    has_key: bool
    source: str  # "db" | "env" | "none"
    masked: str
    last_validated_at: datetime | None = None
    last_validation_ok: bool | None = None
    last_validation_message: str | None = None
    updated_at: datetime | None = None


class LLMKeyUpsert(BaseModel):
    api_key: str  # plaintext; encrypted server-side


class LLMKeyValidation(BaseModel):
    ok: bool
    message: str
