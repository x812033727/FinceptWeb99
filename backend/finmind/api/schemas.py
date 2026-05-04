"""Pydantic response models for the FinMind clone API.

The FinMind canonical API returns:
  { "status": 200, "msg": "success", "data": [ {...}, ... ] }

We mirror that shape in `DataResponse` so customers migrating from
FinMind don't have to reshape their callers. `metadata` is a clone-
specific addition (last_ingest_at, source, freshness) that lets
clients reason about staleness without hitting a separate endpoint.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DataResponseMetadata(BaseModel):
    """Per-response provenance — clone-specific addition vs. FinMind."""

    dataset_code: str
    local_table: str
    active_source: str
    last_ingest_at: datetime | None
    row_count: int


class DataResponse(BaseModel):
    """Mirror of FinMind's canonical wrapper shape, plus `metadata`."""

    status: int = 200
    msg: str = "success"
    data: list[dict[str, Any]] = Field(default_factory=list)
    metadata: DataResponseMetadata


class DatasetSourceItem(BaseModel):
    """One row in `GET /admin/datasets`."""

    dataset_code: str
    category: str
    description_zh: str
    local_table: str
    per_symbol: bool
    primary_source: str
    fallback_source: str | None
    active_source: str
    enabled: bool
    sponsor_tier: bool
    ingest_freq: str
    last_ingest_at: datetime | None
    last_ingest_rows: int | None
    last_error: str | None


class DatasetSourceUpdate(BaseModel):
    """PATCH body for `/admin/datasets/{code}`. All fields optional —
    operator may toggle just `enabled` or just flip `active_source`."""

    enabled: bool | None = None
    active_source: str | None = None


class PublicCatalogItem(BaseModel):
    """Slim, marketing-safe view of a dataset for `/catalog`. Drops
    operator-only fields (last_error, last_ingest_at, primary_source,
    fallback_source, active_source) and surfaces a single `available`
    flag derived from `enabled AND local_table != ''`."""

    dataset_code: str
    category: str
    description_zh: str
    ingest_freq: str
    per_symbol: bool
    sponsor_tier: bool
    available: bool
