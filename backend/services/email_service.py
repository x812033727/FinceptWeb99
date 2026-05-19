"""Email service — currently used only by the daily auto-run
discussion report (per-user opt-in via
`discussion_auto_run_configs.send_email`).

Design:
- Hard fail-closed gate via `is_configured()`. When SMTP_HOST or
  SMTP_FROM is empty the caller skips with a log warning instead
  of crashing the cron — same shape as the news-sentiment Redis
  fail-closed pattern. Local dev / `docker-compose up` without an
  SMTP relay therefore never breaks the auto-run task.
- `send_email()` is async (aiosmtplib) so it doesn't block the
  per-user cron loop. Per-message timeout from
  `settings.SMTP_TIMEOUT_SECONDS`.
- `render_discussion_report_markdown()` is pure — given a
  Discussion ORM row + its conclusion dict + the per-round turns,
  produces a Markdown body suitable for email. Kept here next to
  the sender so the cron task only needs to import this one
  module.
"""
from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Any, Iterable

import aiosmtplib

from config import settings
from models.discussion import Discussion, DiscussionTurn

log = logging.getLogger(__name__)


def is_configured() -> bool:
    """True iff the minimum SMTP envelope (host + from-address) is set.

    Username + password are optional — some relays (in-cluster
    Postfix, AWS SES via IAM, etc.) authenticate at the network
    layer instead of via SMTP AUTH.
    """
    return bool(settings.SMTP_HOST.strip()) and bool(settings.SMTP_FROM.strip())


async def send_email(
    *,
    to: str,
    subject: str,
    body_markdown: str,
    attachment_filename: str | None = None,
    attachment_content: str | None = None,
) -> None:
    """Send a single email. Body is sent as `text/plain` (Markdown
    source — most mail clients render the headers & lists readably
    as-is). Optionally attaches one file (UTF-8 text content), used
    by the auto-run cron to attach the full discussion as a `.md`.

    Raises whatever aiosmtplib raises on transport / auth failure.
    Callers (cron tasks) are expected to catch + log + continue
    so a single user's bad address doesn't poison the loop.
    """
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body_markdown)

    if attachment_filename and attachment_content is not None:
        msg.add_attachment(
            attachment_content.encode("utf-8"),
            maintype="text",
            subtype="markdown",
            filename=attachment_filename,
        )

    # aiosmtplib's `use_tls=True` means *implicit* TLS (port 465);
    # `start_tls=True` means STARTTLS upgrade after plaintext greet
    # (port 587). Match our config flags accordingly. Both default
    # off => plaintext relay (e.g. internal Postfix on 25).
    await aiosmtplib.send(
        msg,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER or None,
        password=settings.SMTP_PASSWORD or None,
        use_tls=bool(settings.SMTP_USE_SSL),
        start_tls=bool(settings.SMTP_USE_TLS) and not bool(settings.SMTP_USE_SSL),
        timeout=settings.SMTP_TIMEOUT_SECONDS,
    )


# ── Markdown rendering ───────────────────────────────────────────


def _fmt_pct(v: Any) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_float(v: Any, places: int = 2) -> str:
    try:
        return f"{float(v):.{places}f}"
    except (TypeError, ValueError):
        return "—"


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def render_discussion_report_markdown(
    discussion: Discussion,
    conclusion: dict[str, Any] | None,
    turns: Iterable[DiscussionTurn],
    *,
    persona_name: dict[str, str] | None = None,
) -> str:
    """Render one discussion as a self-contained Markdown report.

    `persona_name` maps persona_id → display name; missing entries
    fall back to the raw id. Conclusion shape mirrors the synthesizer
    JSON contract (recommended_symbols / reasoning / risks /
    time_horizon / consensus_score, plus the per-symbol
    `recommendations` array when present).
    """
    name = dict(persona_name or {})

    def _pname(pid: str) -> str:
        return name.get(pid, pid)

    created_tw = discussion.created_at.strftime("%Y-%m-%d") if discussion.created_at else "—"
    lines: list[str] = []
    lines.append(f"# 每日專家圓桌討論 — {created_tw}")
    lines.append("")
    lines.append(f"- **市場**: {discussion.market}")
    lines.append(f"- **主題**: {discussion.topic}")
    lines.append(f"- **討論 ID**: `{discussion.id}`")
    if discussion.persona_ids:
        roster = "、".join(_pname(p) for p in discussion.persona_ids)
        lines.append(f"- **出席專家**: {roster}")
    lines.append("")
    lines.append("## 規則")
    lines.append("")
    lines.append("```")
    lines.append((discussion.rules or "").strip())
    lines.append("```")
    lines.append("")

    if conclusion:
        lines.append("## 結論")
        lines.append("")
        symbols = _as_list(conclusion.get("recommended_symbols"))
        if symbols:
            lines.append(f"- **推薦標的**: {', '.join(str(s) for s in symbols)}")
        time_horizon = conclusion.get("time_horizon")
        if time_horizon:
            lines.append(f"- **時間視角**: {time_horizon}")
        consensus = conclusion.get("consensus_score")
        if consensus is not None:
            lines.append(f"- **共識分數**: {_fmt_float(consensus, 2)}")
        reasoning = (conclusion.get("reasoning") or "").strip()
        if reasoning:
            lines.append("")
            lines.append("**主要推論**")
            lines.append("")
            lines.append(reasoning)
        risks = _as_list(conclusion.get("risks"))
        if risks:
            lines.append("")
            lines.append("**風險**")
            lines.append("")
            for r in risks:
                lines.append(f"- {r}")
        recommendations = _as_list(conclusion.get("recommendations"))
        if recommendations:
            lines.append("")
            lines.append("**個別推薦**")
            lines.append("")
            lines.append("| 標的 | 信心 | 校準後信心 | 理由 |")
            lines.append("|---|---|---|---|")
            for rec in recommendations:
                if not isinstance(rec, dict):
                    continue
                sym = str(rec.get("symbol") or "")
                conf = _fmt_pct(rec.get("confidence"))
                calib = _fmt_pct(rec.get("calibrated_confidence"))
                reason = (str(rec.get("reasoning") or "")
                          .replace("|", "\\|").replace("\n", " "))
                lines.append(f"| {sym} | {conf} | {calib} | {reason} |")
        lines.append("")
    else:
        lines.append("## 結論")
        lines.append("")
        lines.append("_（本次討論未產生結論）_")
        lines.append("")

    turns_list = sorted(
        list(turns), key=lambda t: (t.round or 0, t.turn_index or 0),
    )
    if turns_list:
        lines.append("## 各輪發言")
        lines.append("")
        current_round = None
        for turn in turns_list:
            if turn.round != current_round:
                current_round = turn.round
                lines.append(f"### 第 {current_round} 輪")
                lines.append("")
            stance = turn.stance or ""
            who = _pname(turn.persona_id or "")
            lines.append(f"**{who}** _( {stance} )_")
            lines.append("")
            lines.append((turn.content or "").strip())
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
