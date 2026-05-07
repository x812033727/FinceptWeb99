"""Versioned history of strategy artifacts (PR-4a).

PR-C / PR-C2 overwrite the live `persona_weights` and
`calibration_curve` columns on `discussion_strategy_templates` with
each fit. This service adds an append-only history layer:

  - `record_version(...)` is called by the learners (right next to
    the existing in-place overwrite) to capture "what did this
    artifact look like at this fit point". Auto-increments
    `version_number` per (strategy_id, artifact_kind) and flips the
    previous active version to `superseded` in the same transaction.

  - `list_versions(...)` is the read path for the UI history table.

  - `rollback_to_version(...)` lets an operator promote an older
    version back to live. Atomically: writes the historical payload
    back into the template's live column, marks the current active
    row as `rolled_back`, and creates a NEW version row pointing at
    the historical payload (so the rollback itself is auditable).

The live `persona_weights` / `calibration_curve` column on the
template stays the cheap-read source of truth. The version table
is only consulted on rollback or when the operator opens the
history drawer.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_version import StrategyVersion

log = logging.getLogger(__name__)


# Two artifact kinds today; the schema is open so a future fit
# (e.g. prompt template version) can land without a migration.
ARTIFACT_KINDS = ("weights", "calibration_curve")


def _live_column(artifact_kind: str) -> str:
    if artifact_kind == "weights":
        return "persona_weights"
    if artifact_kind == "calibration_curve":
        return "calibration_curve"
    raise ValueError(f"unknown artifact_kind: {artifact_kind!r}")


async def record_version(
    db: AsyncSession,
    *,
    strategy_id: UUID,
    artifact_kind: str,
    payload: Any,
    sample_count: int | None = None,
    source_sweep_id: UUID | None = None,
    notes: str | None = None,
) -> StrategyVersion:
    """Append a new active version + flip the previous active to
    superseded.

    Caller is responsible for the live-column write on the template
    (we don't touch it here — keeps the historical record neutral
    even if the caller chose to park the fit instead of deploying
    it; PR-3's pending mechanism is one such caller path).

    Race-safe under transaction: the version_number is computed via
    `max(version_number) + 1` over the current transaction's view.
    A second concurrent fit would block on the row-level lock acquired
    by the UPDATE that flips superseded.
    """
    if artifact_kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact_kind: {artifact_kind!r}")
    # Compute next version_number
    next_v = await db.scalar(
        select(func.coalesce(func.max(StrategyVersion.version_number), 0) + 1)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.artifact_kind == artifact_kind,
        )
    )
    # Flip the previous active row to superseded — exactly one
    # active version per (strategy, kind) at any time.
    await db.execute(
        update(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.artifact_kind == artifact_kind,
            StrategyVersion.status == "active",
        )
        .values(status="superseded")
    )
    row = StrategyVersion(
        strategy_id=strategy_id,
        version_number=int(next_v),
        artifact_kind=artifact_kind,
        payload=deepcopy(payload),
        sample_count=sample_count,
        source_sweep_id=source_sweep_id,
        status="active",
        notes=notes,
        fit_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_versions(
    db: AsyncSession,
    *,
    strategy_id: UUID,
    artifact_kind: str | None = None,
    limit: int = 20,
) -> list[StrategyVersion]:
    """Newest-first version history for the UI table.

    Pass `artifact_kind=None` to list both weights + curves
    interleaved (chronological); typical UI calls scope by kind
    to keep the table focused.
    """
    q = (
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.fit_at.desc())
        .limit(min(max(int(limit), 1), 200))
    )
    if artifact_kind is not None:
        if artifact_kind not in ARTIFACT_KINDS:
            raise ValueError(f"unknown artifact_kind: {artifact_kind!r}")
        q = q.where(StrategyVersion.artifact_kind == artifact_kind)
    return list((await db.scalars(q)).all())


async def rollback_to_version(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
    version_id: UUID,
) -> dict[str, Any]:
    """Promote a historical version back to live. Atomic + auditable:

      1. Verify the version_id belongs to a strategy this owner has
         access to (via the template's owner_id — soft-deleted
         strategies are still operable to support recovery flows).
      2. Mark the current active version of the same artifact_kind
         as `rolled_back`.
      3. Write the historical payload into the template's live
         column.
      4. INSERT a NEW version row carrying the historical payload
         + `notes="rolled back from v{N}"` so the rollback action
         itself is in the history (lets diagnostics distinguish a
         "rolled back" vs "freshly fitted" identical payload).

    Returns:
      {
        "rolled_back_to_version": int,   # the historical version's number
        "new_active_version": int,       # the new version number created here
        "artifact_kind": str,
      }

    Raises ValueError when the version doesn't exist, doesn't belong
    to this strategy, or the strategy isn't owned by the caller.
    """
    target = await db.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == version_id,
            StrategyVersion.strategy_id == strategy_id,
        )
    )
    if target is None:
        raise ValueError("version not found for this strategy")

    tmpl = await db.scalar(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.id == strategy_id,
            DiscussionStrategyTemplate.owner_id == owner_id,
        )
    )
    if tmpl is None:
        raise ValueError("strategy template not found or not owned by caller")

    if target.status == "active":
        # No-op rollback to the version that's already live.
        return {
            "rolled_back_to_version": target.version_number,
            "new_active_version": target.version_number,
            "artifact_kind": target.artifact_kind,
            "no_op": True,
        }

    # Mark current active as rolled_back
    await db.execute(
        update(StrategyVersion)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.artifact_kind == target.artifact_kind,
            StrategyVersion.status == "active",
        )
        .values(status="rolled_back")
    )

    # Write the historical payload back to the live column
    setattr(
        tmpl, _live_column(target.artifact_kind),
        deepcopy(target.payload),
    )
    if target.artifact_kind == "calibration_curve":
        # Mirror the legacy timestamp + sample_count surface on
        # the template so consumers reading those columns get a
        # consistent state.
        tmpl.calibration_updated_at = datetime.now(UTC)
        tmpl.calibration_sample_count = target.sample_count

    # Create the new audit row pointing at the same payload
    next_v = await db.scalar(
        select(func.coalesce(func.max(StrategyVersion.version_number), 0) + 1)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.artifact_kind == target.artifact_kind,
        )
    )
    audit_row = StrategyVersion(
        strategy_id=strategy_id,
        version_number=int(next_v),
        artifact_kind=target.artifact_kind,
        payload=deepcopy(target.payload),
        sample_count=target.sample_count,
        source_sweep_id=target.source_sweep_id,
        status="active",
        notes=f"rolled back from v{target.version_number}",
        fit_at=datetime.now(UTC),
    )
    db.add(audit_row)
    await db.commit()
    await db.refresh(audit_row)

    return {
        "rolled_back_to_version": target.version_number,
        "new_active_version": audit_row.version_number,
        "artifact_kind": target.artifact_kind,
        "no_op": False,
    }


def version_to_dict(v: StrategyVersion) -> dict[str, Any]:
    """Serialise for API responses."""
    return {
        "id": str(v.id),
        "strategy_id": str(v.strategy_id),
        "version_number": v.version_number,
        "artifact_kind": v.artifact_kind,
        "payload": v.payload,
        "sample_count": v.sample_count,
        "source_sweep_id": str(v.source_sweep_id) if v.source_sweep_id else None,
        "fit_at": v.fit_at.isoformat() if v.fit_at else None,
        "status": v.status,
        "notes": v.notes,
    }


__all__ = [
    "ARTIFACT_KINDS",
    "record_version",
    "list_versions",
    "rollback_to_version",
    "version_to_dict",
]
