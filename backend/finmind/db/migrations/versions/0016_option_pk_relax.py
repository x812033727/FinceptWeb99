"""relax option-table PKs to match FinMind response shape

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-05

Two `tw_option_*` tables had PK columns FinMind doesn't actually ship,
which blocked their datasets from ingest. Both tables have zero rows
in production (confirmed before running), so the cleanest fix is to
drop the unused columns and re-anchor the PK on what FinMind does
emit.

## tw_option_settlement (TaiwanOptionFinalSettlementPrice)

FinMind ships `{date, contract_month, option_type, option_id,
option_name, settlement_price, underlying_code, notional_value}` —
notably NO strike, NO call_put. Settlement is reported per-(option,
contract_month), not per-(option, strike, call_put). The local PK
required strike + call_put, so the dataset was unreachable.

  Before: PK (contract, settlement_date, strike, call_put)
  After : PK (contract, contract_month)

`settlement_date` becomes a regular column (one settlement per
contract_month, so it's functionally derived from the PK and could
even be dropped, but kept for query convenience). `strike` and
`call_put` are dropped — they were never populated.

## tw_option_dealer_volume (TaiwanOptionDealerTradingVolumeDaily)

FinMind ships `{date, dealer_code, dealer_name, option_id, volume,
is_after_hour}` — no call_put. The PK required call_put, so the
dataset was unreachable.

  Before: PK (ts, dealer_id, contract, call_put)
  After : PK (ts, dealer_id, contract)

`call_put` is dropped. The `is_after_hour` axis is folded into the
volume column by aggregation in the mapping's batch_transform, same
pattern as `_batch_futures_dealer_volume`.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # tw_option_settlement: drop strike + call_put, add contract_month,
    # re-anchor PK on (contract, contract_month).
    op.execute("ALTER TABLE tw_option_settlement DROP CONSTRAINT pk_tw_option_settlement")
    op.execute("ALTER TABLE tw_option_settlement DROP COLUMN strike")
    op.execute("ALTER TABLE tw_option_settlement DROP COLUMN call_put")
    op.add_column(
        "tw_option_settlement",
        sa.Column("contract_month", sa.String(length=20), nullable=False, server_default=""),
    )
    # Drop the temporary default — empty string would let unrelated
    # rows collide on the new PK; future inserts must supply the
    # actual contract_month from FinMind.
    op.execute("ALTER TABLE tw_option_settlement ALTER COLUMN contract_month DROP DEFAULT")
    op.create_primary_key(
        "pk_tw_option_settlement",
        "tw_option_settlement",
        ["contract", "contract_month"],
    )

    # tw_option_dealer_volume: drop call_put, re-anchor PK without it.
    op.execute("ALTER TABLE tw_option_dealer_volume DROP CONSTRAINT pk_tw_option_dealer_volume")
    op.execute("ALTER TABLE tw_option_dealer_volume DROP COLUMN call_put")
    op.create_primary_key(
        "pk_tw_option_dealer_volume",
        "tw_option_dealer_volume",
        ["ts", "dealer_id", "contract"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # tw_option_dealer_volume: restore call_put + original PK.
    op.execute("ALTER TABLE tw_option_dealer_volume DROP CONSTRAINT pk_tw_option_dealer_volume")
    op.add_column(
        "tw_option_dealer_volume",
        sa.Column("call_put", sa.String(length=4), nullable=False, server_default=""),
    )
    op.execute("ALTER TABLE tw_option_dealer_volume ALTER COLUMN call_put DROP DEFAULT")
    op.create_primary_key(
        "pk_tw_option_dealer_volume",
        "tw_option_dealer_volume",
        ["ts", "dealer_id", "contract", "call_put"],
    )

    # tw_option_settlement: drop contract_month, restore strike + call_put.
    op.execute("ALTER TABLE tw_option_settlement DROP CONSTRAINT pk_tw_option_settlement")
    op.execute("ALTER TABLE tw_option_settlement DROP COLUMN contract_month")
    op.add_column(
        "tw_option_settlement",
        sa.Column("strike", sa.Numeric(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tw_option_settlement",
        sa.Column("call_put", sa.String(length=4), nullable=False, server_default=""),
    )
    op.execute("ALTER TABLE tw_option_settlement ALTER COLUMN strike DROP DEFAULT")
    op.execute("ALTER TABLE tw_option_settlement ALTER COLUMN call_put DROP DEFAULT")
    op.create_primary_key(
        "pk_tw_option_settlement",
        "tw_option_settlement",
        ["contract", "settlement_date", "strike", "call_put"],
    )
