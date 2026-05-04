"""Daily quota counter + per-key 429 enforcement.

The schema landed in Phase 4 (`api_usage_events` hypertable +
`plans.quota_daily_calls` / `quota_daily_rows`). This module is the
runtime that consumes them:

  - `record_usage(session, api_key_id, ...)` — INSERT one row per
    request after the response is built. Cheap (single insert) and
    survives the request crashing because the middleware writes it
    in a `try/finally`.
  - `current_daily_usage(session, api_key_id)` — SUM(calls, rows)
    for the current UTC day. Read by `check_quota`.
  - `check_quota(session, api_key)` → `QuotaStatus` — reads the
    plan + today's usage and returns whether the next call is
    permitted plus the remaining budget for `Retry-After` /
    `X-RateLimit-Remaining` headers.

UTC day boundary is the cheap-but-fair choice: simple to reason
about ("how many calls today?") and matches FinMind's own
`finmind_counter` Redis pattern in the existing connector. A
calendar-month roll-up for invoicing is a separate cron over the
same hypertable rows.

Phase 4-stub auth (FINMIND_API_KEYS_ALLOWLIST env var) is exempt
from quota — operators know what they're doing when they set the
allowlist directly. Real `fck_live_` keys go through this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.models.billing import ApiKey, ApiUsageEvent, Plan, Subscription


@dataclass
class DailyUsage:
    calls: int
    rows: int


@dataclass
class QuotaStatus:
    """Returned by `check_quota` — the dependency consults this to
    decide 200 vs 429, and copies the headline numbers into response
    headers (X-RateLimit-Limit / -Remaining / -Reset)."""

    permitted: bool
    plan_code: str | None
    daily_call_limit: int
    daily_row_limit: int
    calls_used: int
    rows_used: int
    reset_at: datetime  # next UTC midnight

    @property
    def calls_remaining(self) -> int:
        return max(0, self.daily_call_limit - self.calls_used)

    @property
    def rows_remaining(self) -> int:
        return max(0, self.daily_row_limit - self.rows_used)

    @property
    def reason(self) -> str | None:
        if self.permitted:
            return None
        if self.calls_used >= self.daily_call_limit:
            return (
                f"daily call quota exhausted "
                f"({self.calls_used}/{self.daily_call_limit})"
            )
        if self.rows_used >= self.daily_row_limit:
            return (
                f"daily row quota exhausted "
                f"({self.rows_used}/{self.daily_row_limit})"
            )
        return None


def _utc_today_window() -> tuple[datetime, datetime]:
    """Half-open interval [today_00:00 UTC, tomorrow_00:00 UTC)."""
    now = datetime.now(tz=timezone.utc)
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


async def record_usage(
    session: AsyncSession,
    *,
    api_key_id: int | None,
    dataset_code: str | None,
    endpoint: str,
    row_count: int,
    bytes_out: int,
    latency_ms: int | None,
    status_code: int,
    error_message: str | None = None,
) -> None:
    """Append-only — one row per request. `api_key_id=None` is allowed
    for unauthenticated dev-mode requests so usage analytics still
    capture them; quota check skips when there's no key to charge.

    Best-effort: the caller (middleware/dep) wraps this in try/except
    so a transient DB error during logging never tanks the actual
    response."""
    session.add(
        ApiUsageEvent(
            ts=datetime.now(tz=timezone.utc),
            api_key_id=api_key_id,
            dataset_code=dataset_code,
            endpoint=endpoint,
            row_count=row_count,
            bytes_out=bytes_out,
            latency_ms=latency_ms,
            status_code=status_code,
            error_message=error_message,
        )
    )
    await session.commit()


async def current_daily_usage(
    session: AsyncSession, api_key_id: int
) -> DailyUsage:
    start, end = _utc_today_window()
    result = (
        await session.execute(
            select(
                func.count().label("calls"),
                func.coalesce(func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.api_key_id == api_key_id)
            .where(ApiUsageEvent.ts >= start)
            .where(ApiUsageEvent.ts < end)
            # Only successful + bounded-cost requests count toward
            # quota. 4xx / 5xx don't burn the user's budget — a
            # systemic outage shouldn't cost them their daily allowance.
            .where(ApiUsageEvent.status_code < 400)
        )
    ).one()
    return DailyUsage(calls=int(result[0] or 0), rows=int(result[1] or 0))


# Defaults applied when an API key has no subscription / plan attached
# yet (e.g. the operator issued a key for testing but hasn't created a
# subscription row). Conservative — the operator can override per-key
# via `api_keys.scope_datasets` plus a real plan once billing is wired.
_FREE_TIER_CALL_LIMIT = 100
_FREE_TIER_ROW_LIMIT = 10_000


async def _resolve_plan_limits(
    session: AsyncSession, api_key: ApiKey
) -> tuple[str | None, int, int]:
    """(plan_code, call_limit, row_limit). Free-tier defaults when
    the key has no subscription or the subscription has no plan row."""
    if api_key.subscription_id is None:
        return None, _FREE_TIER_CALL_LIMIT, _FREE_TIER_ROW_LIMIT

    sub = await session.get(Subscription, api_key.subscription_id)
    if sub is None or sub.status not in ("trial", "active"):
        return None, _FREE_TIER_CALL_LIMIT, _FREE_TIER_ROW_LIMIT

    plan = await session.get(Plan, sub.plan_code)
    if plan is None or not plan.enabled:
        return sub.plan_code, _FREE_TIER_CALL_LIMIT, _FREE_TIER_ROW_LIMIT

    return plan.code, plan.quota_daily_calls, plan.quota_daily_rows


async def check_quota(
    session: AsyncSession, api_key: ApiKey
) -> QuotaStatus:
    """Read plan limits + today's usage and decide permitted. Pure
    function from (plan, usage) → QuotaStatus — the request handler
    converts to 200 / 429 + headers."""
    plan_code, call_limit, row_limit = await _resolve_plan_limits(
        session, api_key
    )
    usage = await current_daily_usage(session, api_key.id)

    permitted = (
        usage.calls < call_limit and usage.rows < row_limit
    )

    _, end = _utc_today_window()
    return QuotaStatus(
        permitted=permitted,
        plan_code=plan_code,
        daily_call_limit=call_limit,
        daily_row_limit=row_limit,
        calls_used=usage.calls,
        rows_used=usage.rows,
        reset_at=end,
    )
