"""Billing / subscription / API-key tables — Phase 4 sketch.

Closes the loop on "FinMind clone as a paid service":

  - `plans`            One row per pricing tier. `allowed_datasets`
                       lists which dataset codes the tier can read.
                       NULL/empty allowlist = all datasets.
  - `subscriptions`    Owner ↔ plan mapping with started_at / expires_at.
                       `external_sub_id` is the Stripe / 綠界 sub id;
                       webhook flows update `status` here.
  - `api_keys`         Per-customer API keys. `prefix` shown to the
                       user (`fck_live_abc12345...`); `key_hash` is
                       sha256 of the full key. Lookup is O(1) via
                       `prefix` index, then hash compare.
  - `api_usage_events` Hypertable — every API call writes one row.
                       Used for billing roll-ups (rows / call counts)
                       and for the eventual UsageCard admin UI.

This file lands the schema. Concrete services (Stripe webhook
receiver, key issuance, usage roll-up cron) are explicit Phase 4
follow-ups; ORM here is enough for the auth dep upgrade in
`api.auth` to query `api_keys` instead of an env-var allowlist.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from finmind.db.base import Base

_JSON = JSONB().with_variant(JSON(), "sqlite")


class Plan(Base):
    """One row per pricing tier. Add new plans without code change."""

    __tablename__ = "plans"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    price_monthly: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_yearly: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="TWD")
    # When NULL or empty array, the plan can read every dataset; when
    # non-empty, only the listed dataset_codes are allowed.
    allowed_datasets: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    quota_daily_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    quota_daily_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Subscription(Base):
    """Owner ↔ plan mapping. `owner_email` keeps Phase 4 self-contained
    (the main app's `users` table lives in a different DB); a future
    consolidation can add a `user_id` column."""

    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False)
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # 'trial' | 'active' | 'past_due' | 'canceled' | 'expired'
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Stripe `sub_xxx` / 綠界 / 藍新 reference id — opaque, used to
    # de-duplicate webhook events.
    external_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    external_sub_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_subscriptions_owner_email", "owner_email"),
        Index(
            "ix_subscriptions_external",
            "external_provider", "external_sub_id",
        ),
    )


class ApiKey(Base):
    """Customer-facing API keys.

    Storage shape mirrors GitHub PATs / Stripe keys:
      - User sees `fck_live_<random32>` once at creation
      - We store `prefix` (`fck_live_<first8>`) plus sha256(`full key`)
      - Lookup at request time: `WHERE prefix = ?` (O(1) via index),
        then hash-compare the rest

    `subscription_id` ties the key to a billing plan; on subscription
    expiry the auth dep checks `subscription.status` and 402-rejects
    expired plans without needing a key revocation cycle."""

    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), nullable=True
    )
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Per-key scope override (subset of plan.allowed_datasets). NULL
    # means inherit the plan's full allowlist.
    scope_datasets: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    # Optional IP allowlist — empty = any IP.
    allowed_ips: Mapped[list[str] | None] = mapped_column(_JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_api_keys_prefix", "prefix"),
        Index("ix_api_keys_owner_email", "owner_email"),
    )


class ApiUsageEvent(Base):
    """One row per API call. Hypertable — partitioned by `ts` weekly.

    Used by:
      - daily quota counter rollup (cron summarizes rows from last 24h
        into `api_quota_counters` to drive 429 enforcement)
      - admin UsageCard ("which keys burned the most in the last 7d?")
      - billing month-end ("how many rows did key X read in March?")
    """

    __tablename__ = "api_usage_events"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    api_key_id: Mapped[int | None] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), nullable=True
    )
    dataset_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    bytes_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_api_usage_events_key_ts",
            "api_key_id", "ts",
        ),
        Index(
            "ix_api_usage_events_ts",
            "ts",
        ),
    )


class PaymentEvent(Base):
    """Stripe / 綠界 / 藍新 webhook inbox. We dedupe by
    `(provider, event_id)` so a webhook retry doesn't double-count;
    `processed_at` flips when the consumer actually applied the
    state change (subscription created / canceled / payment_failed)."""

    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_payment_events_provider_event_id",
            "provider", "event_id",
            unique=True,
        ),
    )
