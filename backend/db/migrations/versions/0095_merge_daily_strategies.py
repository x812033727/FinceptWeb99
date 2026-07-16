"""merge daily strategies 5 -> 3 (chip_quality, price_signal)

Revision ID: 0095
Revises: 0094

chip_momentum + quality_growth merge into chip_quality (intersection
strategy) and breakout + oversold_reversal merge into price_signal
(two-track union strategy). Each merged run count takes the max of its
two source counts — a user who ran either old strategy keeps the same
daily volume for the merged one.

Historical `discussions.auto_run_strategy` values keep their old key
strings on purpose; the public API groups them dynamically.

The downgrade is lossy: max() cannot be split back, so both old keys
receive the merged value, which can inflate the total daily run count.
Roll back the application code before downgrading — old code rejects
the merged keys as unknown strategies.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """UPDATE discussion_auto_run_configs
        SET strategy_run_counts = json_build_object(
          'general', COALESCE((strategy_run_counts::jsonb->>'general')::int, 0),
          'chip_quality', LEAST(5, GREATEST(
            COALESCE((strategy_run_counts::jsonb->>'chip_momentum')::int, 0),
            COALESCE((strategy_run_counts::jsonb->>'quality_growth')::int, 0))),
          'price_signal', LEAST(5, GREATEST(
            COALESCE((strategy_run_counts::jsonb->>'breakout')::int, 0),
            COALESCE((strategy_run_counts::jsonb->>'oversold_reversal')::int, 0)))
        )"""
    )


def downgrade() -> None:
    op.execute(
        """UPDATE discussion_auto_run_configs
        SET strategy_run_counts = json_build_object(
          'general', COALESCE((strategy_run_counts::jsonb->>'general')::int, 0),
          'chip_momentum', COALESCE((strategy_run_counts::jsonb->>'chip_quality')::int, 0),
          'quality_growth', COALESCE((strategy_run_counts::jsonb->>'chip_quality')::int, 0),
          'breakout', COALESCE((strategy_run_counts::jsonb->>'price_signal')::int, 0),
          'oversold_reversal', COALESCE((strategy_run_counts::jsonb->>'price_signal')::int, 0)
        )"""
    )
