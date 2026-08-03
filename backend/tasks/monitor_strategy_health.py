"""Daily strategy health monitoring (PR-4b).

Once per UTC day (02:00) iterate every non-stale strategy template,
compute its rolling-30 metrics via `strategy_health_service`, and
persist a snapshot. A snapshot whose non-empty `status_flags`
represent a healthy→degraded TRANSITION (the previous snapshot had
no flags) fires an owner notification through the existing
`notification_service` AND an `alert_events` history row (PR-D4)
so degradation surfaces in the AlertsPage / daily digest without
re-alerting every day for a still-degraded strategy.

Multi-pod safe via the same Redis SET-NX lock pattern as
`score_news_sentiment` — without it, every pod would race to write
the same `(strategy_id, snapshot_date)` and one pod's PK conflict
would log spurious errors. With the lock, exactly one pod runs the
sweep per cron tick.

Runs cheap: a few aggregate queries per strategy + one upsert. A
deployment with ~100 strategies finishes in seconds.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.alert import AlertEvent
from models.discussion import Discussion
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_health_metric import StrategyHealthMetric
from services import strategy_health_service as hsvc
from services import strategy_maturity_service as msvc
from services.ingest.repository import record_health
from services.notification_service import notify_user
from services.veto_clause import (
    VETO_DOWNGRADE_ADOPTED_AT,
    VETO_DOWNGRADE_CLAUSE,
)
from services.veto_guard import _DECIDED, abstention_leakage, revert_trigger

log = logging.getLogger(__name__)

JOB_ID = "monitor_strategy_health"
_LOCK_KEY = "lock:monitor_strategy_health"
_LOCK_TTL = 30 * 60   # 30 min — comfortable for ~100 strategies

# Veto-downgrade revert guard + leakage watch (spec Part 1 governance).
# See `services.veto_guard` for the pure trigger logic. Gated on the
# clause actually being adopted somewhere (`_veto_clause_armed` below)
# — pre-adoption there is nothing to revert or leak, and running the
# queries anyway means one ordinary losing streak on a strategy the
# clause was never applied to fires a daily alarm naming a revert for a
# clause that was never in play.
_LIVE_PRICE_SIGNAL_LIMIT = 15
_LEAKAGE_STRATEGIES = ("chip_quality", "general")
_LEAKAGE_MIN_SAMPLES = 5   # noise floor — skip a window this thin
_LEAKAGE_CURRENT_DAYS = 14
_LEAKAGE_BASELINE_DAYS = 30


async def _alert_owner(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    strategy_name: str,
    flags: list[str],
) -> None:
    """Push one notification per (owner, strategy, flag-set). Best-
    effort: a notification dispatch failure shouldn't block the
    snapshot writes for the rest of the cohort."""
    if not flags:
        return
    try:
        await notify_user(
            str(owner_id),
            {
                "kind": "strategy_health_alert",
                "strategy_id": str(strategy_id),
                "strategy_name": strategy_name,
                "status_flags": flags,
            },
        )
    except Exception as exc:
        log.warning(
            "monitor_strategy_health.alert_failed",
            extra={
                "strategy_id": str(strategy_id),
                "owner_id": str(owner_id),
                "error": str(exc),
            },
        )


async def _record_alert_event(
    *,
    owner_id: UUID,
    strategy_id: UUID,
    strategy_name: str,
    market: str,
    flags: list[str],
) -> None:
    """Persist one `alert_events` history row (PR-D4) so strategy-
    health degradations show up in the AlertsPage 歷史 list and the
    daily digest alongside price alerts. Best-effort, same rationale
    as `_alert_owner`."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(AlertEvent(
                user_id=owner_id,
                alert_id=None,
                symbol=strategy_name[:64],
                market=market,
                kind="strategy_health",
                message=(
                    f"策略「{strategy_name}」健康度劣化:"
                    f"{', '.join(flags)}"
                ),
                payload={
                    "strategy_id": str(strategy_id),
                    "status_flags": flags,
                },
            ))
            await db.commit()
    except Exception as exc:
        log.warning(
            "monitor_strategy_health.alert_event_failed",
            extra={
                "strategy_id": str(strategy_id),
                "owner_id": str(owner_id),
                "error": str(exc),
            },
        )


async def _abstain_rate(
    db: AsyncSession, *, strategy: str, window_start: datetime, window_end: datetime,
) -> tuple[float | None, int]:
    """abstain-count / decision-verdict-count for one auto_run_strategy
    over a `[window_start, window_end)` slice of live (as_of_date IS
    NULL) discussions. Returns `(rate, sample_count)`; rate is None
    when the window has no decision verdicts at all.

    `unverifiable` is a data-availability bucket, not a decision — same
    convention as `daily_scoreboard_service`'s win-rate denominator
    (WINNING_VERDICTS/LOSING_VERDICTS exclude it too). Left in the
    denominator here, a spike in unverifiable rows (e.g. a data-source
    outage) would dilute the abstain-rate and could trip a false
    leakage finding unrelated to any veto-clause bleed."""
    verdicts = list((await db.scalars(
        select(Discussion.verdict).where(
            Discussion.auto_run_strategy == strategy,
            Discussion.as_of_date.is_(None),
            Discussion.auto_run.is_(True),
            Discussion.verdict.is_not(None),
            Discussion.verdict != "unverifiable",
            Discussion.created_at >= window_start,
            Discussion.created_at < window_end,
        )
    )).all())
    total = len(verdicts)
    if total == 0:
        return (None, 0)
    abstains = sum(1 for v in verdicts if v == "abstain")
    return (abstains / total, total)


async def _veto_clause_armed(db: AsyncSession) -> bool:
    """True when at least one `DiscussionAutoRunConfig.rules` contains
    the veto-downgrade clause — i.e. it has actually been adopted
    somewhere, not merely available to apply. Gates the entire guard
    section: `_veto_guard_findings` below has no business running
    against live data for a clause nobody has applied yet."""
    row = await db.scalar(
        select(DiscussionAutoRunConfig.user_id).where(
            DiscussionAutoRunConfig.rules.contains(VETO_DOWNGRADE_CLAUSE)
        ).limit(1)
    )
    return row is not None


async def _veto_guard_findings(db: AsyncSession) -> list[str]:
    """Post-adoption tripwires for the price_signal macro-veto
    downgrade (spec Part 1): a revert-condition check on the newest
    live price_signal verdicts, plus an abstention-leakage watch on
    the other auto-run strategies (a prompt-scoped veto clause
    bleeding into their reasoning shows up as their abstain rate
    collapsing). Pure judgment lives in `services.veto_guard`; this
    function only gathers the data and formats findings."""
    findings: list[str] = []

    # Filter to decided verdicts IN SQL, not in revert_trigger (which
    # already re-filters, harmlessly, once its input is already
    # decided-only). Filtering in Python after a plain `verdict IS NOT
    # NULL` + LIMIT 15 fetch let abstains/unverifiable rows crowd the
    # 15-row window on an abstain-heavy tape, leaving revert_trigger's
    # rolling-10 slice with fewer than 10 actual decided verdicts to
    # look at. Filtering decided-only in SQL first means the LIMIT 15
    # is 15 decided verdicts, always enough for the rolling-10 check.
    verdicts = list((await db.scalars(
        select(Discussion.verdict).where(
            Discussion.auto_run_strategy == "price_signal",
            Discussion.as_of_date.is_(None),
            Discussion.auto_run.is_(True),
            Discussion.verdict.in_(_DECIDED),
            # Only picks that ran under the downgraded ruleset are in
            # scope for a revert judgment — see VETO_DOWNGRADE_ADOPTED_AT.
            Discussion.created_at >= VETO_DOWNGRADE_ADOPTED_AT,
        )
        .order_by(Discussion.created_at.desc())
        .limit(_LIVE_PRICE_SIGNAL_LIMIT)
    )).all())
    trigger = revert_trigger(verdicts)
    if trigger:
        findings.append(trigger)

    now = datetime.now(UTC)
    current_start = now - timedelta(days=_LEAKAGE_CURRENT_DAYS)
    baseline_start = current_start - timedelta(days=_LEAKAGE_BASELINE_DAYS)
    for strategy in _LEAKAGE_STRATEGIES:
        current_rate, current_n = await _abstain_rate(
            db, strategy=strategy, window_start=current_start, window_end=now,
        )
        baseline_rate, baseline_n = await _abstain_rate(
            db, strategy=strategy,
            window_start=baseline_start, window_end=current_start,
        )
        if current_n < _LEAKAGE_MIN_SAMPLES or baseline_n < _LEAKAGE_MIN_SAMPLES:
            continue
        if abstention_leakage(current_rate=current_rate, baseline_rate=baseline_rate):
            findings.append(
                f"Abstention leakage on {strategy}: abstain rate dropped "
                f"from {baseline_rate:.0%} (prior 30d) to {current_rate:.0%} "
                f"(last 14d) — check for cross-strategy veto-clause bleed."
            )
    return findings


async def run_health_monitor() -> dict:
    """Returns a counters dict for the IngestHealthCard."""
    counters = {
        "strategies_total": 0,
        "snapshots_written": 0,
        "alerts_fired": 0,
        "errors": 0,
        "guard_findings": [],
    }

    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("monitor_strategy_health.skipped (lock held)")
        return {**counters, "skipped": "lock_held"}

    try:
        async with AsyncSessionLocal() as db:
            strategies = list((await db.scalars(
                select(DiscussionStrategyTemplate).where(
                    DiscussionStrategyTemplate.deleted_at.is_(None),
                )
            )).all())
        counters["strategies_total"] = len(strategies)

        for tmpl in strategies:
            # Stale strategies skip the snapshot — they would just
            # produce all-NULL rows and bloat the table. The
            # maturity check below still updates the tier so a
            # stale row doesn't get stuck mid-flag.
            try:
                async with AsyncSessionLocal() as db:
                    tier, _ = await msvc.update_maturity_tier(
                        db, strategy_id=tmpl.id,
                    )
                if tier == "stale":
                    continue

                # Last-known state BEFORE today's upsert — the newest
                # existing snapshot row (yesterday's on a normal daily
                # tick, today's own row on a same-day re-run). Non-empty
                # flags there mean "already degraded", so we only alert
                # on the healthy→degraded TRANSITION instead of
                # re-alerting every run for a still-degraded strategy.
                async with AsyncSessionLocal() as db:
                    prev_flags = await db.scalar(
                        select(StrategyHealthMetric.status_flags)
                        .where(StrategyHealthMetric.strategy_id == tmpl.id)
                        .order_by(StrategyHealthMetric.snapshot_date.desc())
                        .limit(1)
                    )
                was_degraded = bool(prev_flags)

                async with AsyncSessionLocal() as db:
                    row = await hsvc.record_snapshot(
                        db, strategy_id=tmpl.id,
                    )
                counters["snapshots_written"] += 1

                flags = list(row.status_flags or [])
                if flags and not was_degraded:
                    await _record_alert_event(
                        owner_id=tmpl.owner_id,
                        strategy_id=tmpl.id,
                        strategy_name=tmpl.name,
                        market=tmpl.market,
                        flags=flags,
                    )
                    await _alert_owner(
                        owner_id=tmpl.owner_id,
                        strategy_id=tmpl.id,
                        strategy_name=tmpl.name,
                        flags=flags,
                    )
                    counters["alerts_fired"] += 1
            except Exception as exc:
                counters["errors"] += 1
                log.warning(
                    "monitor_strategy_health.strategy_failed",
                    extra={
                        "strategy_id": str(tmpl.id),
                        "error": str(exc),
                    },
                )

        # Veto-downgrade revert guard + leakage watch — independent of
        # the per-strategy sweep above, so its own try/except keeps a
        # guard-side failure from ever masking the monitor's existing
        # duties (snapshots still get written even if this errors).
        try:
            async with AsyncSessionLocal() as db:
                if await _veto_clause_armed(db):
                    counters["guard_findings"] = await _veto_guard_findings(db)
        except Exception as exc:
            # A guard-side failure must not read as a silent green: it's
            # exactly the kind of dead tripwire that hides a real revert
            # condition behind a query bug. Fold it into guard_findings
            # (health_monitor_job's `ok` already keys off `not
            # guard_findings`) instead of only logging a warning nobody
            # is watching.
            counters["guard_findings"] = [
                *counters["guard_findings"],
                f"veto_guard check failed: {exc}",
            ]
            log.warning(
                "monitor_strategy_health.veto_guard_failed",
                extra={"error": str(exc)},
            )
    finally:
        await release_lock(_LOCK_KEY)

    return counters


async def health_monitor_job() -> None:
    """APScheduler entry — wraps `run_health_monitor` with the
    standard health-row recording + structured logging surface so
    the IngestHealthCard sees the same shape as every other
    background job."""
    started = datetime.now(UTC)
    try:
        result = await run_health_monitor()
        guard_findings = result.get("guard_findings") or []
        ok = result.get("errors", 0) == 0 and not guard_findings
        message = (
            f"strategies={result['strategies_total']} "
            f"snapshots={result['snapshots_written']} "
            f"alerts={result['alerts_fired']} "
            f"errors={result['errors']}"
        )
        if guard_findings:
            message += " | veto_guard: " + " | ".join(guard_findings)
        if result.get("skipped"):
            message = f"skipped (reason: {result['skipped']})"
        await record_health(
            JOB_ID, ok=ok, row_count=result["snapshots_written"],
            error=None if ok else message,
        )
        log.info(
            "monitor_strategy_health.complete",
            extra={
                "duration_s": (
                    datetime.now(UTC) - started
                ).total_seconds(),
                **result,
            },
        )
    except Exception as exc:
        log.exception("monitor_strategy_health.failed")
        try:
            await record_health(JOB_ID, ok=False, error=str(exc))
        except Exception:
            pass
