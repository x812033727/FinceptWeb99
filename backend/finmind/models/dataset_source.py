"""dataset_sources — single routing table for the FinMind clone.

One row per FinMind dataset (84 total at time of writing — see
`data.tw.finmind_datasets.all_datasets()`). Each row records:

  - which local table the dataset's payload lands in (`local_table`)
  - which upstream source the ingest layer should currently use
    (`active_source`: 'finmind' during Phase A; 'twse' / 'tpex' / etc.
    after subscription expiry)
  - last-run telemetry (`last_ingest_at`, `last_ingest_rows`,
    `last_error`) so the admin UI can render an at-a-glance health grid

Phase-A → Phase-B switching is a single UPDATE on `active_source` —
no code change, no migration. Seed defaults are populated by
`finmind.scripts.init_db` reading from `finmind.dataset_catalog.CATALOG`.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class DatasetSource(Base):
    __tablename__ = "dataset_sources"

    # FinMind dataset name verbatim — matches DatasetSpec.name in
    # `data.tw.finmind_datasets`. Typos here = silent ingest failures.
    dataset_code: Mapped[str] = mapped_column(String(64), primary_key=True)

    # 'technical' | 'chip' | 'fundamental' | 'derivative'
    # | 'realtime' | 'convertible_bond' | 'other'
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    description_zh: Mapped[str] = mapped_column(String(128), nullable=False)

    # Empty string when the destination table hasn't been migrated yet
    # (Phase 1 fills these in batch by batch). Ingest layer skips rows
    # with empty `local_table`.
    local_table: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=""
    )

    per_symbol: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # 'finmind' | 'twse' | 'tpex' | 'taifex' | 'mops' | 'tdcc'
    primary_source: Mapped[str] = mapped_column(String(16), nullable=False)
    fallback_source: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Currently-selected ingest source; defaults to primary at seed time.
    # Admin UI flips this when transitioning Phase A → Phase B per dataset.
    active_source: Mapped[str] = mapped_column(String(16), nullable=False)

    # FinMind sponsor-only datasets (tick / 5-sec / etc.). Cosmetic only —
    # the actual quota logic lives in `data.tw.finmind_connector`.
    # `false()` renders portably (FALSE on PG, 0 on SQLite) — a literal
    # "false" string would be stored as the text "false" by SQLite, which
    # is *truthy* and silently breaks `WHERE NOT sponsor_tier`.
    sponsor_tier: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )

    # Off by default until Phase 1 wires the destination table + ingest
    # task. Admin flips on per-dataset to opt in.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )

    # 'daily' | 'monthly' | 'quarterly' | 'realtime' | 'on_demand'
    ingest_freq: Mapped[str] = mapped_column(String(16), nullable=False)

    # Run telemetry — written by ingest workers, read by AdminPage.
    last_ingest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_ingest_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
