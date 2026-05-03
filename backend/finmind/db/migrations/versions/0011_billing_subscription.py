"""billing / subscription / api_keys / usage events

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-03

Phase 4. Five tables to close the loop on "FinMind clone as a paid
service": plans, subscriptions, api_keys, api_usage_events,
payment_events. See `finmind/models/billing.py` for the model-level
docs and roll-up strategy.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_timescaledb() -> bool:
    if op.get_bind().dialect.name != "postgresql":
        return False
    return bool(
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
        )
        .scalar()
    )


def _json() -> sa.types.TypeEngine:
    return JSONB().with_variant(JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("price_monthly", sa.Numeric(10, 2), nullable=True),
        sa.Column("price_yearly", sa.Numeric(10, 2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="TWD"),
        sa.Column("allowed_datasets", _json(), nullable=True),
        sa.Column("quota_daily_calls", sa.Integer(), nullable=False),
        sa.Column("quota_daily_rows", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name="pk_plans"),
    )

    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("owner_email", sa.String(length=255), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.Date(), nullable=True),
        sa.Column("external_provider", sa.String(length=16), nullable=True),
        sa.Column("external_sub_id", sa.String(length=64), nullable=True),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
    )
    op.create_index(
        "ix_subscriptions_owner_email", "subscriptions", ["owner_email"]
    )
    op.create_index(
        "ix_subscriptions_external",
        "subscriptions",
        ["external_provider", "external_sub_id"],
    )

    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_email", sa.String(length=255), nullable=False),
        sa.Column(
            "subscription_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=True),
        sa.Column("scope_datasets", _json(), nullable=True),
        sa.Column("allowed_ips", _json(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_index("ix_api_keys_owner_email", "api_keys", ["owner_email"])

    op.create_table(
        "api_usage_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "api_key_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            nullable=True,
        ),
        sa.Column("dataset_code", sa.String(length=64), nullable=True),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_out", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_usage_events"),
    )
    op.create_index(
        "ix_api_usage_events_key_ts",
        "api_usage_events",
        ["api_key_id", "ts"],
    )
    op.create_index("ix_api_usage_events_ts", "api_usage_events", ["ts"])

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable('api_usage_events', 'ts', "
            "chunk_time_interval => INTERVAL '1 week', "
            "if_not_exists => TRUE)"
        )

    op.create_table(
        "payment_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_payment_events"),
    )
    op.create_index(
        "ix_payment_events_provider_event_id",
        "payment_events",
        ["provider", "event_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payment_events_provider_event_id", table_name="payment_events"
    )
    op.drop_table("payment_events")
    op.drop_index("ix_api_usage_events_ts", table_name="api_usage_events")
    op.drop_index("ix_api_usage_events_key_ts", table_name="api_usage_events")
    op.drop_table("api_usage_events")
    op.drop_index("ix_api_keys_owner_email", table_name="api_keys")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_subscriptions_external", table_name="subscriptions")
    op.drop_index("ix_subscriptions_owner_email", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plans")
