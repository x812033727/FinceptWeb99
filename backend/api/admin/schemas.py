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


# ── Market-data provider keys ────────────────────────────────────

class MarketKeyInfo(BaseModel):
    """Same shape as LLMKeyInfo, for keys consumed by data/* connectors
    (Finnhub today; Polygon / FRED / FinMind are candidates for follow-up)."""
    provider: str
    has_key: bool
    source: str  # "db" | "env" | "none"
    masked: str
    last_validated_at: datetime | None = None
    last_validation_ok: bool | None = None
    last_validation_message: str | None = None
    updated_at: datetime | None = None


class MarketKeyUpsert(BaseModel):
    api_key: str  # plaintext; encrypted server-side


class MarketKeyValidation(BaseModel):
    ok: bool
    message: str


# ── Per-persona model routing ────────────────────────────────────

class PersonaConfigOut(BaseModel):
    persona_id: str
    name: str
    description: str
    default_provider: str
    default_model: str
    effective_provider: str
    effective_model: str
    is_overridden: bool


class PersonaOverrideIn(BaseModel):
    provider: str
    model: str


# ── LLM usage summary ────────────────────────────────────────────

class UsageBucketOut(BaseModel):
    period: str
    provider: str
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class UsageDayPoint(BaseModel):
    date: str
    cost_usd: float
    requests: int


class UsageSummaryOut(BaseModel):
    range_days: int
    user_scoped: bool
    total_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float
    by_provider: list[UsageBucketOut]
    by_day: list[UsageDayPoint]


# ── Scheduled ingest health ──────────────────────────────────────

class IngestHealthOut(BaseModel):
    """One row per scheduled ingest job for the admin dashboard."""
    job_id: str
    last_run_at: str | None
    ok: bool
    row_count: int
    error: str | None = None
