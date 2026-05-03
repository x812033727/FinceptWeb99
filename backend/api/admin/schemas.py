import uuid
from datetime import datetime

from typing import Any

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


# ── Background task model routing ─────────────────────────────────

class SystemTaskConfigOut(BaseModel):
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


class SystemTaskOverrideIn(BaseModel):
    provider: str
    model: str


class SystemTaskTestResult(BaseModel):
    ok: bool
    provider: str
    model: str
    latency_ms: int
    sample_output: str | None = None
    error: str | None = None


# ── Runtime-tunable settings (env-var overrides) ─────────────────

class RuntimeSettingOut(BaseModel):
    key: str
    type: str           # "int" | "float" | "bool" | "str"
    name: str
    description: str
    min_value: float | int | None = None
    max_value: float | int | None = None
    default_value: Any
    effective_value: Any
    is_overridden: bool
    updated_at: datetime | None = None
    updated_by_email: str | None = None


class RuntimeSettingIn(BaseModel):
    value: Any


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


class IngestRetryResult(BaseModel):
    status: str
    message: str


# ── Signal-citation audit ────────────────────────────────────────

class SignalCoverageRow(BaseModel):
    """One row in the audit-coverage table — citation rate for a single
    signal across the audit window."""
    signal: str
    present: int           # rounds the signal appeared in
    cited: int             # persona-turn keyword citations
    # PR #258: subset of `cited` where the turn ALSO contained a
    # numeric token within ±60 chars of the keyword. Distinguishes
    # "RSI 67.3" citations from "RSI 偏高" name-drops. Always
    # ≤ `cited`.
    cited_with_value: int = 0
    persona_count: int     # denominator for citation_rate
    citation_rate: float   # cited / persona_count, 0.0 when persona_count=0
    value_citation_rate: float = 0.0   # cited_with_value / persona_count


class PostMortemGapRow(BaseModel):
    """PR #262: per-category aggregate of "missing data" mentions
    extracted from post-mortem persona turns."""
    category: str
    mentions: int                  # raw turn-level matches
    discussions_mentioning: int    # distinct discussions w/ ≥1 hit
    persona_count_mentioning: int  # distinct personas w/ ≥1 hit


class PostMortemGapsOut(BaseModel):
    """Bulk roll-up surfaced to the AdminPage. Sorted descending by
    mentions so the most-requested gaps surface first."""
    discussions_audited: int
    discussion_ids: list[str]
    gaps: list[PostMortemGapRow]


class SignalHallucinationRow(BaseModel):
    """PR #259: per-signal hallucination row. A signal is "hallucinated"
    when a persona cites it WITH a numeric value despite the round's
    prompt context not including that signal — i.e., the persona
    fabricated data we didn't supply. Sorted descending by rate so
    the worst offenders surface first."""
    signal: str
    absent_rounds: int            # rounds where signal was missing from prompt
    hallucinated: int             # turns that cited it with a value anyway
    persona_count_absent: int     # denominator
    hallucination_rate: float     # hallucinated / persona_count_absent


class SignalAuditHistoryPoint(BaseModel):
    """PR #263: one day in the per-signal trend. Pre-computed rates
    so the sparkline renderer doesn't need div-by-zero handling."""
    captured_at: str
    discussions_audited: int
    present_count: int
    cited_count: int
    cited_with_value_count: int
    hallucinated_count: int
    persona_count: int
    persona_count_absent: int
    citation_rate: float
    value_citation_rate: float
    hallucination_rate: float


class SignalAuditHistoryOut(BaseModel):
    """Per-signal daily history. Sorted ascending by date so the
    frontend can plot left-to-right."""
    signal: str
    market: str | None
    points: list[SignalAuditHistoryPoint]


class SignalAuditOut(BaseModel):
    """Bulk audit result surfaced to the AdminPage. Sorted ascending
    by `citation_rate` so zero-uptake offenders surface first."""
    discussions_audited: int
    discussion_ids: list[str]
    coverage: list[SignalCoverageRow]
    zero_uptake: list[str]   # signals present ≥1 round but never cited
    # PR #259: only signals with hallucinated > 0 surface here. Empty
    # list = personas stayed inside the supplied data.
    hallucinations: list[SignalHallucinationRow] = []
    # PR #263: per-signal daily history points for the sparkline
    # column. Keyed by signal name; empty dict when no snapshots
    # exist yet (the cron hasn't run, or runs with limited backfill).
    history: dict[str, list[SignalAuditHistoryPoint]] = {}


# ── Empirical signal quality vs D5 returns ───────────────────────


class NumericSignalRow(BaseModel):
    """One row in the numeric-signal correlation table."""
    label: str
    path: str
    n: int
    mean_value: float
    mean_d5_return: float
    pearson_r: float | None
    sign_match_pct: float | None


class CategoricalSignalBucket(BaseModel):
    """Per-category aggregate for a categorical signal."""
    category: str
    n: int
    mean_d5_return: float
    median_d5_return: float


class CategoricalSignalRow(BaseModel):
    label: str
    path: str
    n_total: int
    buckets: list[CategoricalSignalBucket]


class SignalQualityOut(BaseModel):
    """Empirical signal-vs-D5-return report. Numeric signals get
    Pearson correlation + sign-match accuracy; categorical signals
    get per-bucket mean D5. Reads only — derived from concluded
    discussions' `daily_close_prices` paired with their round-1
    context snapshots."""
    discussions_audited: int
    discussion_ids: list[str]
    lookback_days: int
    market: str | None
    numeric: list[NumericSignalRow]
    categorical: list[CategoricalSignalRow]
