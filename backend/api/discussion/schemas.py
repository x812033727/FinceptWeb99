"""Pydantic schemas for the discussion API.

Kept deliberately small — the full Discussion + Turn rows include
internal columns (owner_id, ingested_at) that the API doesn't need to
expose, so we project them through these response models.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateDiscussionRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    rules: str = Field(min_length=1, max_length=2000)
    persona_ids: list[str] = Field(min_length=2, max_length=8)
    # Market the discussion is anchored to. Optional for backwards
    # compat with clients that pre-date the field — `discussion_service`
    # falls back to the default ('TW') when None / missing. Validation
    # against `_VALID_MARKETS` happens in the service layer so the
    # allowed-set lives in one place.
    market: str | None = Field(default=None, max_length=8)


class UpdateDiscussionRequest(BaseModel):
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    rules: str | None = Field(default=None, min_length=1, max_length=2000)
    persona_ids: list[str] | None = Field(default=None, min_length=2, max_length=8)
    market: str | None = Field(default=None, max_length=8)


class TurnResponse(BaseModel):
    id: int
    round: int
    turn_index: int
    persona_id: str
    stance: str
    content: str
    citations: dict[str, Any] | None = None
    created_at: datetime


class DiscussionResponse(BaseModel):
    id: uuid.UUID
    topic: str
    rules: str
    persona_ids: list[str]
    market: str
    status: str
    current_round: int
    conclusion: dict[str, Any] | None = None
    # Self-grading fields (migration 0018). All optional / default-None
    # so legacy clients ignore them; frontend uses them to render the
    # dynamic title (YYYYMMDD(syms)勝/敗) instead of the user-typed topic.
    verdict: str | None = None
    verdict_reason: str | None = None
    verified_at: datetime | None = None
    auto_run: bool = False
    # Per-symbol price snapshots so the frontend can render
    # `4958:55/51 (-7.3%)` per recommended symbol without re-fetching
    # the OHLCV bars. Captured by the verifier task once the 5-day
    # window closes.
    day1_open_prices: dict[str, float] | None = None
    day5_close_prices: dict[str, float] | None = None
    # Per-day closes from the scoreboard cron (PR #140). Shape:
    # `{symbol: [d1, d2, d3, d4, d5]}` with NULL slots for unresolved
    # days. Surfaced so the sidebar title can render the latest
    # non-null close even before D5 lands, instead of showing `—/—`
    # until the full window completes.
    daily_close_prices: dict[str, list[float | None]] | None = None
    created_at: datetime
    updated_at: datetime


class DiscussionDetailResponse(DiscussionResponse):
    turns: list[TurnResponse]


class ConclusionResponse(BaseModel):
    discussion_id: uuid.UUID
    conclusion: dict[str, Any]


class AutoRunConfigRequest(BaseModel):
    enabled: bool
    persona_ids: list[str] = Field(min_length=2, max_length=8)
    topic: str = Field(min_length=1, max_length=500)
    rules: str = Field(min_length=1, max_length=2000)
    market: str | None = Field(default=None, max_length=8)


class AutoRunConfigResponse(BaseModel):
    enabled: bool
    persona_ids: list[str]
    topic: str
    rules: str
    market: str
    updated_at: datetime | None = None


class ScoreboardRow(BaseModel):
    symbol: str
    day1_open: float | None
    daily_closes: list[float | None]
    change_pcts: list[float | None]
    days_resolved: int


class ScoreboardResponse(BaseModel):
    discussion_id: uuid.UUID
    created_at_tw_date: str
    rows: list[ScoreboardRow]
