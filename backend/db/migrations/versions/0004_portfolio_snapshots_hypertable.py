"""portfolio_snapshots → TimescaleDB hypertable

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-25

Q1 epic (#3) — convert portfolio_snapshots to a hypertable so per-portfolio
time-range queries (`snapshot_date BETWEEN ... AND ...`) hit a single chunk
instead of scanning the whole table.

Schema reshape:
  - PK changes from (id) to (snapshot_date, id) so TimescaleDB's
    "partitioning column must be in every UNIQUE index" rule is satisfied
    without dropping the surrogate UUID id.
  - The existing UNIQUE(portfolio_id, snapshot_date) already includes
    snapshot_date — no change needed there.

Hypertable conversion is wrapped in a TimescaleDB-presence check so plain
PostgreSQL (and SQLite, which never runs Alembic in tests) work unchanged.
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _has_timescaledb() -> bool:
    if not _is_postgres():
        return False
    return bool(op.get_bind().exec_driver_sql(
        "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
    ).scalar())


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("ALTER TABLE portfolio_snapshots DROP CONSTRAINT portfolio_snapshots_pkey")
    op.execute(
        "ALTER TABLE portfolio_snapshots ADD CONSTRAINT portfolio_snapshots_pkey "
        "PRIMARY KEY (snapshot_date, id)"
    )

    if _has_timescaledb():
        op.execute(
            "SELECT create_hypertable("
            "'portfolio_snapshots', 'snapshot_date', "
            "chunk_time_interval => INTERVAL '1 year', "
            "migrate_data => TRUE, "
            "if_not_exists => TRUE)"
        )


def downgrade() -> None:
    if not _is_postgres():
        return

    op.execute("ALTER TABLE portfolio_snapshots DROP CONSTRAINT portfolio_snapshots_pkey")
    op.execute(
        "ALTER TABLE portfolio_snapshots ADD CONSTRAINT portfolio_snapshots_pkey "
        "PRIMARY KEY (id)"
    )
