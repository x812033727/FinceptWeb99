"""llm_usage_events — tool_call_count + tool_call_breakdown

Revision ID: 0055
Revises: 0054
Create Date: 2026-05-07

Persona tool-use was opaque to billing/observability before this. The
existing schema only tracked `prompt_tokens` + `completion_tokens` per
chat completion, but a single persona turn that goes through the
OpenAI-compat tool loop can fire multiple LLM round-trips plus N tool
invocations — and the row-level "why did this round take 12 minutes"
question had no answer without parsing logs.

This migration adds two columns:

  - `tool_call_count` — total tool invocations during the request
    (sum across all loop iterations). Default 0, NOT NULL so existing
    pre-PR rows show as "no tool use" rather than "unknown".

  - `tool_call_breakdown` — per-tool-name counts as JSON, e.g.
    `{"get_quote": 3, "get_options_chain": 1}`. Nullable; NULL means
    "no breakdown captured" (back-compat with rows from before this
    migration). New rows always write `{}` at minimum when no tools
    fire.

Both surfaces feed UsageCard's per-persona drill-down + AdminPage's
"why is this discussion slow" diagnostic without changing the existing
prompt/completion token contract.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage_events",
        sa.Column(
            "tool_call_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "llm_usage_events",
        sa.Column(
            "tool_call_breakdown",
            sa.JSON(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("llm_usage_events", "tool_call_breakdown")
    op.drop_column("llm_usage_events", "tool_call_count")
