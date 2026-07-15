from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StockPickCandidate(BaseModel):
    rank: int
    symbol: str
    market: str
    score: float
    stance: str
    confidence: float
    rationale: str
    source_report_id: str
    report_quality: float
    report_created_at: str
    quality_details: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class StockPickRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    market: str
    run_date: date
    methodology_version: str
    status: str
    candidate_count: int
    candidates: list[StockPickCandidate]
    source_report_ids: list[str]
    generated_at: datetime


class LatestStockPickRuns(BaseModel):
    runs: list[StockPickRunOut]
    disclaimer: str = "Research candidates only; not investment advice."
