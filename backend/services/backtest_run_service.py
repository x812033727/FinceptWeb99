"""C3 — persisted backtest runs: save / list / get / delete / compare.

Every helper is strictly user-scoped: a run that exists but belongs to
another user behaves exactly like a run that doesn't exist (404 at the
API layer), so run ids leak nothing.

Compare semantics (`compare_runs`)
──────────────────────────────────
Runs may cover different date ranges and different capital bases, so
raw equity values are not comparable. Each run's equity curve is
normalised to **100 at its own first bar** (value / first_value × 100)
and the curves are aligned on the sorted union of all dates; a run
contributes ``None`` on dates outside its own range (no interpolation —
the frontend renders gaps rather than inventing data). Runs whose
first bar value is 0 (fully degenerate) normalise to an all-``None``
series rather than dividing by zero.
"""
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_run import MAX_PERSISTED_TRADES, BacktestRun

MAX_COMPARE_RUNS = 4


def cap_trades(
    trades: list[dict] | None, total_trades: int | None = None,
) -> tuple[list[dict] | None, bool]:
    """Keep at most the last MAX_PERSISTED_TRADES trades.

    Returns (capped_trades, truncated). `total_trades` (the engine's
    authoritative count from metrics) also marks truncation when the
    engine itself already trimmed the list it returned.
    """
    if not trades:
        return None, bool(total_trades and total_trades > 0)
    capped = trades[-MAX_PERSISTED_TRADES:]
    truncated = len(trades) > len(capped)
    if total_trades is not None and total_trades > len(capped):
        truncated = True
    return capped, truncated


async def save_run(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    name: str | None,
    strategy: str,
    params: dict[str, Any],
    config: dict[str, Any],
    result: dict[str, Any],
) -> BacktestRun:
    """Persist one completed backtest result. Caller guarantees
    `result["status"] == "completed"`."""
    metrics = result.get("metrics") or {}
    trades, truncated = cap_trades(
        result.get("trades"), metrics.get("total_trades"),
    )
    run = BacktestRun(
        user_id=user_id,
        name=(name or None),
        strategy=strategy,
        params=params,
        config={**config, "trades_truncated": truncated},
        metrics=metrics,
        equity_curve=result.get("equity_curve") or [],
        trades=trades,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def list_runs(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int = 20, offset: int = 0,
) -> tuple[list[BacktestRun], int]:
    """Caller's runs, newest first, plus the total count for paging."""
    total = (
        await db.execute(
            select(func.count())
            .select_from(BacktestRun)
            .where(BacktestRun.user_id == user_id)
        )
    ).scalar_one()
    rows = (
        await db.scalars(
            select(BacktestRun)
            .where(BacktestRun.user_id == user_id)
            .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total)


async def get_run(
    db: AsyncSession, user_id: uuid.UUID, run_id: uuid.UUID,
) -> BacktestRun | None:
    """One run — None when missing OR owned by someone else."""
    row = await db.get(BacktestRun, run_id)
    if row is None or row.user_id != user_id:
        return None
    return row


async def delete_run(
    db: AsyncSession, user_id: uuid.UUID, run_id: uuid.UUID,
) -> bool:
    row = await get_run(db, user_id, run_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


def normalise_curve(equity_curve: list[dict]) -> dict[str, float | None]:
    """Map date → equity normalised to 100 at the curve's first bar."""
    if not equity_curve:
        return {}
    base = float(equity_curve[0].get("value") or 0)
    if base <= 0:
        return {str(p.get("date")): None for p in equity_curve}
    out: dict[str, float | None] = {}
    for p in equity_curve:
        v = p.get("value")
        out[str(p.get("date"))] = (
            round(float(v) / base * 100, 4) if v is not None else None
        )
    return out


async def compare_runs(
    db: AsyncSession, user_id: uuid.UUID, run_ids: list[uuid.UUID],
) -> dict[str, Any] | None:
    """Aligned, normalised curves + side-by-side metrics.

    Returns None when any id is missing / not the caller's — the API
    turns that into a blanket 404. Output order follows `run_ids`.
    """
    runs: list[BacktestRun] = []
    for rid in run_ids:
        row = await get_run(db, user_id, rid)
        if row is None:
            return None
        runs.append(row)

    norm = [normalise_curve(r.equity_curve or []) for r in runs]
    dates = sorted(set().union(*[set(n.keys()) for n in norm])) if norm else []
    return {
        "dates": dates,
        "runs": [
            {
                "id": str(run.id),
                "name": run.name,
                "strategy": run.strategy,
                "created_at": run.created_at,
                "metrics": run.metrics or {},
                "values": [n.get(d) for d in dates],
            }
            for run, n in zip(runs, norm)
        ],
    }
