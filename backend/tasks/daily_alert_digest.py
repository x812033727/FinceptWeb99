"""Daily alert digest email (PR-D5).

Once per UTC day (21:00) aggregate the last 24 h of `alert_events`
for users who explicitly enabled the Email channel's daily-digest
preference, then send one Markdown summary. Default is off.

Fail-closed everywhere, mirroring `auto_run_discussion`'s report
email path:
- SMTP unconfigured (`email_service.is_configured()` false) → skip
  silently with one log line; local dev never crashes the cron.
- Per-user send failure → log + continue, one bad address must not
  poison the loop.

Multi-pod safe via the same Redis SET-NX lock pattern as
`monitor_strategy_health` / `score_news_sentiment`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.alert import AlertEvent
from models.notification_channel import NotificationChannel
from models.user import User
from services import email_service
from services.ingest.repository import record_health

log = logging.getLogger(__name__)

JOB_ID = "daily_alert_digest"
_LOCK_KEY = "lock:daily_alert_digest"
_LOCK_TTL = 30 * 60   # 30 min — generous for a per-user email loop
_WINDOW_HOURS = 24

_KIND_LABEL = {
    "price": "價格警示",
    "strategy_health": "策略健康",
}


def render_digest_markdown(events: list[AlertEvent], *, day: str) -> str:
    """Pure renderer: one user's last-24h events → Markdown body."""
    lines: list[str] = [f"# 每日告警摘要 — {day}", ""]
    lines.append(f"過去 24 小時共觸發 **{len(events)}** 筆告警。")
    lines.append("")
    lines.append("| 時間 (UTC) | 類型 | 標的 | 市場 | 內容 |")
    lines.append("|---|---|---|---|---|")
    for ev in sorted(events, key=lambda e: e.fired_at):
        fired = ev.fired_at.strftime("%m-%d %H:%M") if ev.fired_at else "—"
        kind = _KIND_LABEL.get(ev.kind, ev.kind)
        msg = (ev.message or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {fired} | {kind} | {ev.symbol} | {ev.market} | {msg} |")
    return "\n".join(lines) + "\n"


async def run_daily_alert_digest() -> dict:
    """Returns a counters dict for the IngestHealthCard."""
    counters = {
        "users_with_events": 0,
        "emails_sent": 0,
        "errors": 0,
    }

    # Fail-closed gate BEFORE the lock: a deployment without SMTP
    # never even contends for the lock (same shape as the auto-run
    # email skip).
    if not email_service.is_configured():
        log.warning("daily_alert_digest.skipped_smtp_not_configured")
        return {**counters, "skipped": "smtp_not_configured"}

    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("daily_alert_digest.skipped (lock held)")
        return {**counters, "skipped": "lock_held"}

    try:
        now = datetime.now(UTC)
        since = now - timedelta(hours=_WINDOW_HOURS)
        async with AsyncSessionLocal() as db:
            channels = list((await db.scalars(select(NotificationChannel).where(
                NotificationChannel.kind == "email",
                NotificationChannel.verified.is_(True),
            ))).all())
            opted_in = {
                channel.user_id for channel in channels
                if bool((channel.config or {}).get("daily_digest", False))
            }
            if not opted_in:
                return counters
            events = list((await db.scalars(
                select(AlertEvent)
                .where(
                    AlertEvent.fired_at >= since,
                    AlertEvent.user_id.in_(opted_in),
                )
                .order_by(AlertEvent.user_id, AlertEvent.fired_at)
            )).all())

            by_user: dict = {}
            for ev in events:
                by_user.setdefault(ev.user_id, []).append(ev)
            counters["users_with_events"] = len(by_user)

            day = now.strftime("%Y-%m-%d")
            for user_id, user_events in by_user.items():
                user = await db.scalar(select(User).where(User.id == user_id))
                if user is None or not (user.email or "").strip():
                    continue
                body = render_digest_markdown(user_events, day=day)
                subject = f"[Fincept] 每日告警摘要 {day}({len(user_events)} 筆)"
                try:
                    await email_service.send_email(
                        to=user.email, subject=subject, body_markdown=body,
                    )
                    counters["emails_sent"] += 1
                except Exception as exc:
                    counters["errors"] += 1
                    log.warning(
                        "daily_alert_digest.email_failed",
                        extra={"user_id": str(user_id), "error": str(exc)},
                    )
    finally:
        await release_lock(_LOCK_KEY)

    return counters


async def daily_alert_digest_job() -> None:
    """APScheduler entry — standard health-row recording wrapper so
    the IngestHealthCard sees the same shape as other crons."""
    started = datetime.now(UTC)
    try:
        result = await run_daily_alert_digest()
        ok = result.get("errors", 0) == 0
        message = (
            f"users={result['users_with_events']} "
            f"sent={result['emails_sent']} errors={result['errors']}"
        )
        if result.get("skipped"):
            message = f"skipped (reason: {result['skipped']})"
        await record_health(
            JOB_ID, ok=ok, row_count=result["emails_sent"],
            error=None if ok else message,
        )
        log.info(
            "daily_alert_digest.complete",
            extra={
                "duration_s": (datetime.now(UTC) - started).total_seconds(),
                **result,
            },
        )
    except Exception as exc:
        log.exception("daily_alert_digest.failed")
        try:
            await record_health(JOB_ID, ok=False, error=str(exc))
        except Exception:
            pass
