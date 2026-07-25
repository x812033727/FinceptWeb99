"""Operator tool for the macro-veto downgrade clause (spec Part 1).

Adoption is GATED on the spec's pre-registered criteria — running
--apply is the human act of adoption, after the experiment grades land
in the criteria table. The original text is printed AND written to
docs/rules-archive/ before any change, so revert is always possible
even without this script.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

VETO_DOWNGRADE_CLAUSE = (
    "\n\n【僅適用於量價訊號策略場次】總經逆風(外資台指期淨空、三大法人連續"
    "賣超、台VIX 偏高等系統性風險)不得作為否決個股的唯一理由。當候選同時"
    "滿足技術面與籌碼面進場條件時仍應給出推薦,但總經逆風時必須:(1) 建議"
    "部位上限減半;(2) 停損位收緊並明確標出;(3) 在 risks 首條標注「總經"
    "逆風環境」。僅當個股本身不符技術或籌碼條件、或風報比不足時才棄權。"
)


def apply_clause(rules: str) -> str:
    if VETO_DOWNGRADE_CLAUSE in rules:
        return rules
    return rules + VETO_DOWNGRADE_CLAUSE


def revert_clause(rules: str) -> str:
    return rules.replace(VETO_DOWNGRADE_CLAUSE, "")


async def main(mode: str, force_without_archive: bool) -> None:
    from db.session import AsyncSessionLocal
    from models.discussion_auto_run_config import DiscussionAutoRunConfig

    async with AsyncSessionLocal() as db:
        cfgs = (await db.scalars(select(DiscussionAutoRunConfig))).all()
        for cfg in cfgs:
            rules = cfg.rules or ""
            present = VETO_DOWNGRADE_CLAUSE in rules
            print(f"config user={cfg.user_id} clause_present={present}")
            if mode == "show":
                continue

            # Attempt archive before any DB change
            stamp = datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
            archive = Path("docs/rules-archive")
            try:
                archive.mkdir(parents=True, exist_ok=True)
                archive_file = archive / f"rules-{cfg.user_id}-{stamp}.txt"
                archive_file.write_text(rules)
                print(f"--- archived original ({len(rules)} chars) to {archive_file} ---")
            except Exception as e:
                print(f"!!! archive write failed: {e}")
                print(f"--- original text ({len(rules)} chars) ---")
                print(rules)
                print("--- end original text ---")
                if not force_without_archive:
                    print("Pass --force-without-archive to proceed without archive")
                    continue

            new_rules = (
                apply_clause(rules) if mode == "apply" else revert_clause(rules)
            )
            if new_rules == rules:
                print("no-op (already in desired state)")
                continue

            cfg.rules = new_rules
            await db.commit()
            print(f"{mode} done for user={cfg.user_id}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    ap.add_argument("--force-without-archive", action="store_true",
                    help="proceed with DB update even if archive write fails")
    args = ap.parse_args()
    mode = "apply" if args.apply else "revert" if args.revert else "show"
    asyncio.run(main(mode, args.force_without_archive))
