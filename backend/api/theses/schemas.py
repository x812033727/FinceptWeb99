import math
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WatchCondition(BaseModel):
    """A machine-evaluable thesis condition fed by archived market data."""

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    metric: Literal["revenue_yoy_pct", "revenue_mom_pct", "foreign_net"]
    operator: Literal["lt", "lte", "gt", "gte", "eq"]
    threshold: float

    @field_validator("threshold")
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("threshold must be finite")
        return value


class ThesisCreate(BaseModel):
    market: Literal["TW", "US"]
    symbol: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9.\-]+$")
    title: str = Field(min_length=1, max_length=200)
    core_case: str = Field(min_length=1, max_length=10000)
    catalysts: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    valuation: dict[str, Any] = Field(default_factory=dict)
    # Plain strings remain accepted for existing clients; structured entries
    # are the subset the scheduler can evaluate automatically.
    watch_conditions: list[WatchCondition | str] = Field(default_factory=list, max_length=20)
    review_date: date | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def supported_structured_conditions(self):
        if self.market != "TW" and any(isinstance(item, WatchCondition) for item in self.watch_conditions):
            raise ValueError("structured watch conditions currently require market TW")
        return self


class ThesisUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    status: Literal["active", "watching", "invalidated", "closed"] | None = None
    core_case: str | None = Field(None, min_length=1, max_length=10000)
    catalysts: list[Any] | None = None
    risks: list[Any] | None = None
    valuation: dict[str, Any] | None = None
    watch_conditions: list[WatchCondition | str] | None = Field(None, max_length=20)
    review_date: date | None = None


class ThesisReview(BaseModel):
    conclusion: Literal["unchanged", "strengthened", "weakened", "invalidated"]
    notes: str = Field(min_length=1, max_length=10000)
    next_review_date: date | None = None


class ThesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    market: str
    symbol: str
    title: str
    status: str
    core_case: str
    catalysts: list[Any]
    risks: list[Any]
    valuation: dict[str, Any]
    watch_conditions: list[Any]
    review_date: date | None
    last_reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ThesisEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    event_type: str
    title: str
    details: dict[str, Any]
    source: str | None
    source_ref: str | None
    occurred_at: datetime
    created_at: datetime
