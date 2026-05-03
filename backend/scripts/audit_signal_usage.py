"""Audit which discussion-context signals personas actually cite.

After every PR that adds a new context block, the next question is
whether personas are reading it or just collecting JSON. This script
joins the persisted ``discussion_round_contexts`` with the
``discussion_turns`` rows and prints, per (round, persona, signal),
whether the persona's content keyword-matched the signal at all.

Two modes:

  # Single discussion deep-dive
  python -m scripts.audit_signal_usage --discussion-id <uuid>

  # Roll-up across the last N concluded discussions (any market by
  # default; --market TW filters)
  python -m scripts.audit_signal_usage --recent 30
  python -m scripts.audit_signal_usage --recent 50 --market TW

Output sections:

  1. Round-by-round signal presence (single mode only).
  2. Per-persona citation table (single mode only).
  3. Coverage summary: per signal, citation rate over the full
     discussion (single) or across every audited discussion (bulk).
  4. Zero-uptake list — signals present in ≥ 1 round but NEVER
     cited by any persona. Top suspects for prompt-template tuning
     or persona-profile reassignment.

Reads only — never writes. Safe to run against production.

Detection is keyword/regex (see ``services.signal_audit_service.
_SIGNAL_KEYWORDS``). False negatives possible (a persona citing a
number without the canonical token); the goal is spotting
**zero**-uptake signals, where keyword OR is plenty.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from db.session import AsyncSessionLocal
from services.signal_audit_service import (
    BulkAuditSummary, DiscussionAudit,
    audit_discussion, audit_recent_discussions,
)


def _format_report(audit: DiscussionAudit) -> str:
    """Human-readable report. JSON option could be added later if
    we want to feed this into a CI gate."""
    out: list[str] = []
    out.append(f"\n=== Discussion {audit.discussion_id} ===")
    out.append(f"Topic: {audit.topic}")
    out.append(f"Rounds: {len(audit.rounds)}")
    if not audit.rounds:
        out.append("(no persisted round contexts — older discussion?)")
        return "\n".join(out)

    for r in audit.rounds:
        out.append(f"\n--- Round {r.round} ---")
        out.append(
            f"Signals present in context ({len(r.signals_present)}):"
        )
        for sig in sorted(r.signals_present):
            out.append(f"  • {sig}")
        if not r.turns:
            out.append("(no persona turns recorded)")
            continue
        out.append(f"\nPer-persona citations ({len(r.turns)} personas):")
        for t in r.turns:
            out.append(
                f"  [{t.persona_id}] stance={t.stance} "
                f"cited {t.cited_count}/{len(t.cited)}:"
            )
            for sig in sorted(t.cited):
                mark = "✓" if t.cited[sig] else "✗"
                out.append(f"    {mark} {sig}")

    out.append("\n=== Coverage summary ===")
    coverage = audit.coverage()
    if not coverage:
        out.append("(no signals to summarise)")
    else:
        # Sort by citation rate ascending — least-cited first so the
        # zero-uptake offenders surface at the top.
        rows = []
        for sig, stats in coverage.items():
            denom = stats["persona_count"]
            rate = (stats["cited"] / denom) if denom else 0.0
            rows.append((rate, sig, stats))
        rows.sort(key=lambda x: (x[0], x[1]))
        for rate, sig, stats in rows:
            out.append(
                f"  {rate*100:5.1f}%  "
                f"({stats['cited']}/{stats['persona_count']} cited, "
                f"{stats['present']} rounds)  {sig}"
            )

    zero_uptake = [
        sig for sig, stats in coverage.items()
        if stats["cited"] == 0 and stats["persona_count"] > 0
    ]
    if zero_uptake:
        out.append("\n=== Zero-uptake signals ===")
        out.append(
            "These were present in ≥ 1 round but NEVER cited by any "
            "persona. Candidates for prompt-template tuning or "
            "persona-profile reassignment."
        )
        for sig in sorted(zero_uptake):
            out.append(f"  ! {sig}")
    return "\n".join(out)


def _format_bulk_report(summary: BulkAuditSummary) -> str:
    out: list[str] = []
    out.append("\n=== Bulk audit: recent discussions ===")
    out.append(f"Audited: {summary.discussions_audited} discussions")
    if summary.discussions_audited == 0:
        out.append(
            "(no discussions matched the filters OR none had persisted "
            "round contexts)"
        )
        return "\n".join(out)

    out.append("\n=== Coverage (across all audited discussions) ===")
    if not summary.coverage:
        out.append("(no signals to summarise)")
    else:
        rows = []
        for sig, stats in summary.coverage.items():
            denom = stats["persona_count"]
            rate = (stats["cited"] / denom) if denom else 0.0
            rows.append((rate, sig, stats))
        rows.sort(key=lambda x: (x[0], x[1]))
        for rate, sig, stats in rows:
            out.append(
                f"  {rate*100:5.1f}%  "
                f"({stats['cited']}/{stats['persona_count']} cited, "
                f"present in {stats['present']} round-snapshots)  {sig}"
            )

    zero_uptake = [
        sig for sig, stats in summary.coverage.items()
        if stats["cited"] == 0 and stats["persona_count"] > 0
    ]
    if zero_uptake:
        out.append("\n=== Zero-uptake signals (across the audit window) ===")
        out.append(
            "Present in ≥ 1 round but NEVER cited by any persona in any "
            "audited discussion. Strong prompt-tuning / profile-removal "
            "candidates."
        )
        for sig in sorted(zero_uptake):
            out.append(f"  ! {sig}")
    return "\n".join(out)


async def _run_single(discussion_id: str) -> int:
    try:
        did = uuid.UUID(discussion_id)
    except ValueError:
        print(f"error: --discussion-id must be a UUID, got {discussion_id!r}",
              file=sys.stderr)
        return 2
    async with AsyncSessionLocal() as db:
        audit = await audit_discussion(db, did)
    if audit is None:
        print(
            f"error: discussion {discussion_id} not found or has no "
            "persisted round contexts (older discussions written before "
            "PR #209 don't have them).",
            file=sys.stderr,
        )
        return 1
    print(_format_report(audit))
    return 0


async def _run_bulk(limit: int, market: str | None) -> int:
    async with AsyncSessionLocal() as db:
        summary = await audit_recent_discussions(
            db, limit=limit, market=market,
        )
    print(_format_bulk_report(summary))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--discussion-id",
        help="UUID of a single discussion to deep-dive",
    )
    mode.add_argument(
        "--recent", type=int, metavar="N",
        help="Roll up across the most recent N concluded discussions",
    )
    ap.add_argument(
        "--market",
        help="(bulk only) Filter discussions by market: TW / US / GLOBAL",
    )
    args = ap.parse_args()
    if args.discussion_id:
        if args.market:
            print("warning: --market ignored in single-discussion mode",
                  file=sys.stderr)
        sys.exit(asyncio.run(_run_single(args.discussion_id)))
    else:
        sys.exit(asyncio.run(_run_bulk(args.recent, args.market)))


if __name__ == "__main__":
    main()
