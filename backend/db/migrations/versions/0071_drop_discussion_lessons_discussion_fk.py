"""discussion_lessons: drop the discussion_id -> discussions FK

Revision ID: 0071
Revises: 0070
Create Date: 2026-07-12

Context: the fincept99 test site imported the full lesson library (1086
rows) from the origin site so the whole corpus is browsable here. Those
lessons carry their ORIGINAL `discussion_id` values, but the 238 parent
discussions were intentionally NOT copied (only the library was wanted),
so `fk_discussion_lessons_discussion_id_discussions` can no longer be
satisfied. The LessonLibraryPage is owner-scoped and does not join the
discussions table, so dropping the FK loses nothing the library relies on
— only the per-lesson "open source discussion" drill-down would 404 for
imported rows (and there are no source discussions on this site anyway).
`owner_user_id -> users` is left intact (imported rows were re-owned to a
real local user).

Idempotent: `DROP CONSTRAINT IF EXISTS` so this applies cleanly whether the
FK is still present (fresh DB) or was already dropped out-of-band (the
site where the import was run).

Downgrade re-adds the FK with its original name and ON DELETE CASCADE.
Note: re-adding will fail on any DB that still holds imported lessons whose
`discussion_id` has no matching `discussions` row — that dangling data is
the whole reason the FK was dropped, so a clean downgrade requires those
parent discussions to exist first.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK = "fk_discussion_lessons_discussion_id_discussions"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE discussion_lessons DROP CONSTRAINT IF EXISTS {_FK}"
    )


def downgrade() -> None:
    op.create_foreign_key(
        _FK,
        "discussion_lessons",
        "discussions",
        ["discussion_id"],
        ["id"],
        ondelete="CASCADE",
    )
