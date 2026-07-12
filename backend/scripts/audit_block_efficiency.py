"""Per-block context efficiency report — bytes spent vs. citations earned.

`audit_signal_usage.py` answers "which *signals* do personas cite?".
This script answers the budgeting question one level up: **which context
*blocks* are worth their bytes?** It joins two data sources that live at
different grains:

  1. `discussion_turns.input_breakdown["blocks"]` — per-persona-turn char
     count for each ctx block that survived the persona's profile filter.
  2. `signal_audit_service.audit_recent_discussions(...)` — per-signal
     citation coverage over the same window.

For each block it reports mean bytes (when the block is actually sent),
how often it's sent, its share of total context bytes, and the citation
rate of the signal(s) that block feeds. A block with a large byte share
and ~0 citation is a trim candidate (drop it from personas that never
cite it — G7-1c — or stop building it entirely if no one does — G7-1b).

    python -m scripts.audit_block_efficiency --recent 50
    python -m scripts.audit_block_efficiency --recent 50 --market TW --json

Reads only — never writes. Safe against production.

Block→signal mapping note: most blocks map 1:1 to a signal of the same
name. `short_term_signals` fans out into `short_term_signals.<field>`
sub-signals; this script pools them back to the block. Blocks with no
audited signal (e.g. `recent_lessons`, `top_gainers`, metadata keys) are
reported as "not audited" — bytes are known, citation is not measured.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass

from sqlalchemy import select

from db.session import AsyncSessionLocal
from models.discussion import Discussion, DiscussionTurn
from services.signal_audit_service import audit_recent_discussions

# Metadata keys in input_breakdown["blocks"] that are structural, not
# information blocks — excluded from the "trim candidate" verdict (you
# can't drop `market`/`errors`). Bytes are still reported.
_METADATA_KEYS = frozenset({
    "market", "captured_at", "captured_session", "backtest", "as_of",
    "info_cutoff", "news_backfill", "errors",
})


@dataclass
class BlockStat:
    block: str
    turns_present: int          # turns where this block was sent
    mean_bytes: float           # mean chars when present
    total_bytes: int            # summed chars across all turns
    byte_share_pct: float       # share of all block bytes
    cited_rate: float | None    # pooled citation rate, None if not audited
    value_rate: float | None    # pooled cited-with-value rate
    signal_count: int           # how many audited signals feed this block
    verdict: str


async def _collect_block_bytes(
    db, *, limit: int, market: str | None
) -> tuple[dict[str, list[int]], int]:
    """Return {block_key: [char counts across turns]} + turns scanned.

    Scans the most recent `limit` concluded discussions' turns (matching
    the same window the auditor uses) and pulls each turn's per-block
    char counts out of `input_breakdown`."""
    disc_q = (
        select(Discussion.id)
        .where(Discussion.status == "concluded")
        .order_by(Discussion.created_at.desc())
        .limit(limit)
    )
    if market:
        disc_q = disc_q.where(Discussion.market == market)
    disc_ids = [row[0] for row in (await db.execute(disc_q)).all()]
    if not disc_ids:
        return {}, 0

    turns = (
        await db.execute(
            select(DiscussionTurn.input_breakdown).where(
                DiscussionTurn.discussion_id.in_(disc_ids),
                DiscussionTurn.input_breakdown.is_not(None),
            )
        )
    ).all()

    per_block: dict[str, list[int]] = defaultdict(list)
    scanned = 0
    for (breakdown,) in turns:
        blocks = (breakdown or {}).get("blocks") or {}
        if not blocks:
            continue
        scanned += 1
        for key, chars in blocks.items():
            if isinstance(chars, int):
                per_block[key].append(chars)
    return per_block, scanned


def _pool_citation_by_block(coverage: dict) -> dict[str, dict]:
    """Group per-signal coverage into per-block pooled stats. A signal
    `short_term_signals.rsi_14` pools under block `short_term_signals`;
    a 1:1 signal `taifex_positioning` pools under itself."""
    pooled: dict[str, dict] = defaultdict(
        lambda: {"cited": 0, "cited_with_value": 0,
                 "persona_count": 0, "signals": 0}
    )
    for signal, stats in coverage.items():
        block = signal.split(".", 1)[0]
        p = pooled[block]
        p["cited"] += stats.get("cited", 0)
        p["cited_with_value"] += stats.get("cited_with_value", 0)
        p["persona_count"] += stats.get("persona_count", 0)
        p["signals"] += 1
    return pooled


def _build_stats(
    per_block: dict[str, list[int]], pooled: dict[str, dict]
) -> list[BlockStat]:
    grand_total = sum(sum(v) for v in per_block.values()) or 1
    stats: list[BlockStat] = []
    for block, char_list in per_block.items():
        total = sum(char_list)
        present = len(char_list)
        cov = pooled.get(block)
        if cov and cov["persona_count"]:
            cited_rate = cov["cited"] / cov["persona_count"]
            value_rate = cov["cited_with_value"] / cov["persona_count"]
            sig_count = cov["signals"]
        else:
            cited_rate = value_rate = None
            sig_count = 0

        share = total / grand_total * 100
        # Verdict: flag high-byte / low-citation blocks. Only informational
        # blocks (not metadata) are trim candidates.
        if block in _METADATA_KEYS:
            verdict = "metadata"
        elif cited_rate is None:
            verdict = "not-audited" + (" (heavy)" if share >= 5 else "")
        elif cited_rate == 0:
            verdict = "TRIM: never cited"
        elif share >= 8 and cited_rate < 0.15:
            verdict = "TRIM?: heavy + rarely cited"
        elif cited_rate < 0.1:
            verdict = "low-uptake"
        else:
            verdict = "ok"

        stats.append(BlockStat(
            block=block,
            turns_present=present,
            mean_bytes=round(total / present, 1) if present else 0.0,
            total_bytes=total,
            byte_share_pct=round(share, 2),
            cited_rate=round(cited_rate, 4) if cited_rate is not None else None,
            value_rate=round(value_rate, 4) if value_rate is not None else None,
            signal_count=sig_count,
            verdict=verdict,
        ))
    # Heaviest bytes first — that's where trimming pays off most.
    stats.sort(key=lambda s: -s.total_bytes)
    return stats


def _render(stats: list[BlockStat], scanned: int, audited: int) -> str:
    out: list[str] = []
    out.append("\n=== Per-block context efficiency ===")
    out.append(
        f"turns scanned (with input_breakdown): {scanned}   "
        f"discussions audited for citations: {audited}"
    )
    if not stats:
        out.append("(no input_breakdown data — run some discussions first, "
                   "or widen --recent)")
        return "\n".join(out)
    out.append("")
    out.append(
        f"{'block':32} {'share':>6} {'mean_B':>8} {'sent':>5} "
        f"{'cite':>6} {'value':>6}  verdict"
    )
    out.append("-" * 88)
    for s in stats:
        cite = f"{s.cited_rate*100:5.1f}%" if s.cited_rate is not None else "   -- "
        val = f"{s.value_rate*100:5.1f}%" if s.value_rate is not None else "   -- "
        out.append(
            f"{s.block:32} {s.byte_share_pct:5.1f}% {s.mean_bytes:8.0f} "
            f"{s.turns_present:5d} {cite} {val}  {s.verdict}"
        )
    out.append("")
    trims = [s.block for s in stats if s.verdict.startswith("TRIM")]
    if trims:
        out.append("Trim candidates (heavy bytes, ~0 citation): "
                   + ", ".join(trims))
    return "\n".join(out)


async def _run(limit: int, market: str | None, as_json: bool) -> int:
    async with AsyncSessionLocal() as db:
        per_block, scanned = await _collect_block_bytes(
            db, limit=limit, market=market
        )
        summary = await audit_recent_discussions(db, limit=limit, market=market)
    pooled = _pool_citation_by_block(summary.coverage)
    stats = _build_stats(per_block, pooled)

    if as_json:
        print(json.dumps({
            "turns_scanned": scanned,
            "discussions_audited": summary.discussions_audited,
            "blocks": [asdict(s) for s in stats],
        }, ensure_ascii=False, indent=2))
    else:
        print(_render(stats, scanned, summary.discussions_audited))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--recent", type=int, default=50, metavar="N",
        help="Scan the most recent N concluded discussions (default 50).",
    )
    ap.add_argument(
        "--market", help="Filter by market: TW / US / GLOBAL.",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the human-readable table.",
    )
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.recent, args.market, args.json)))


if __name__ == "__main__":
    main()
