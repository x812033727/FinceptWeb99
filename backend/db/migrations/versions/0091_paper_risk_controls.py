"""add portfolio paper-trading risk controls

Revision ID: 0091
Revises: 0090
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0091"
down_revision: str | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_fills",
        sa.Column("currency", sa.String(length=3), server_default="USD", nullable=False),
    )
    op.add_column(
        "paper_fills",
        sa.Column("realized_pnl", sa.Numeric(18, 6), server_default="0", nullable=False),
    )
    op.execute(
        """
        UPDATE paper_fills
        SET currency = CASE
            WHEN (SELECT market FROM paper_orders WHERE paper_orders.id = paper_fills.order_id) = 'TW'
            THEN 'TWD' ELSE 'USD'
        END
        """
    )
    op.create_check_constraint(
        "ck_paper_fills_currency", "paper_fills", "currency IN ('USD', 'TWD')"
    )

    op.create_table(
        "paper_risk_policies",
        sa.Column(
            "portfolio_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("trading_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("max_order_notional_usd", sa.Numeric(20, 6)),
        sa.Column("max_order_notional_twd", sa.Numeric(20, 6)),
        sa.Column("max_position_notional_usd", sa.Numeric(20, 6)),
        sa.Column("max_position_notional_twd", sa.Numeric(20, 6)),
        sa.Column("max_daily_loss_usd", sa.Numeric(20, 6)),
        sa.Column("max_daily_loss_twd", sa.Numeric(20, 6)),
        sa.Column("max_open_orders", sa.Integer()),
        sa.Column("max_symbol_concentration_pct", sa.Numeric(7, 4)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "max_order_notional_usd IS NULL OR max_order_notional_usd > 0",
            name="ck_paper_risk_order_usd_positive",
        ),
        sa.CheckConstraint(
            "max_order_notional_twd IS NULL OR max_order_notional_twd > 0",
            name="ck_paper_risk_order_twd_positive",
        ),
        sa.CheckConstraint(
            "max_position_notional_usd IS NULL OR max_position_notional_usd > 0",
            name="ck_paper_risk_position_usd_positive",
        ),
        sa.CheckConstraint(
            "max_position_notional_twd IS NULL OR max_position_notional_twd > 0",
            name="ck_paper_risk_position_twd_positive",
        ),
        sa.CheckConstraint(
            "max_daily_loss_usd IS NULL OR max_daily_loss_usd > 0",
            name="ck_paper_risk_daily_loss_usd_positive",
        ),
        sa.CheckConstraint(
            "max_daily_loss_twd IS NULL OR max_daily_loss_twd > 0",
            name="ck_paper_risk_daily_loss_twd_positive",
        ),
        sa.CheckConstraint(
            "max_open_orders IS NULL OR max_open_orders > 0",
            name="ck_paper_risk_open_orders_positive",
        ),
        sa.CheckConstraint(
            "max_symbol_concentration_pct IS NULL OR "
            "(max_symbol_concentration_pct > 0 AND max_symbol_concentration_pct <= 100)",
            name="ck_paper_risk_concentration_range",
        ),
    )


def downgrade() -> None:
    op.drop_table("paper_risk_policies")
    op.drop_constraint("ck_paper_fills_currency", "paper_fills", type_="check")
    op.drop_column("paper_fills", "realized_pnl")
    op.drop_column("paper_fills", "currency")
