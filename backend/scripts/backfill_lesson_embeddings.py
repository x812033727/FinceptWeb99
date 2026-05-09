"""One-shot lesson semantic-embedding backfill (PR-J1).

The inline embedding write that runs alongside `extract_and_persist_
lessons` only covers newly-persisted rows. Existing lessons (everything
written before PR-J1 landed, plus rows persisted while the
LESSON_EMBEDDING_ENABLED switch was OFF) sit with `embedding=NULL`
and won't surface via the semantic-similarity ranking until they
get backfilled.

Run from inside the backend dir:

    # default — backfill every NULL-embedding lesson
    python -m scripts.backfill_lesson_embeddings

    # scope to one market or one user
    python -m scripts.backfill_lesson_embeddings --market TW
    python -m scripts.backfill_lesson_embeddings --owner-user-id <uuid>

    # cap the run (e.g. while testing the integration)
    python -m scripts.backfill_lesson_embeddings --max-rows 200

Idempotent: rows whose `embedding_model` already matches the
configured `LESSON_EMBEDDING_MODEL` are skipped, so re-running the
script is safe + cheap.

Required: `LESSON_EMBEDDING_PROVIDER` resolves to a configured
provider key (default `openai` reads `OPENAI_API_KEY` env / admin-
stored DB row). Empty key = the script logs `skipped_no_api_key`
and exits 0 without doing anything; bumping the cap won't unstick
the run until the key lands.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

from db.session import AsyncSessionLocal
from services.lesson_embedding_service import backfill_pending

log = logging.getLogger(__name__)


def _parse_uuid(s: str) -> uuid.UUID:
    return uuid.UUID(s)


async def _main(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        result = await backfill_pending(
            db,
            owner_user_id=args.owner_user_id,
            market=args.market,
            max_rows=args.max_rows,
        )

    log.info(
        "backfill_lesson_embeddings.done",
        extra={
            "scanned": result.get("scanned", 0),
            "updated": result.get("updated", 0),
            "batches": result.get("batches", 0),
            "skipped": result.get("skipped", False),
        },
    )
    print(  # noqa: T201 — script output, intentional
        f"scanned={result.get('scanned', 0)} "
        f"updated={result.get('updated', 0)} "
        f"batches={result.get('batches', 0)} "
        f"skipped={result.get('skipped', False)}"
    )
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill_lesson_embeddings",
        description=(
            "Walk un-embedded lessons in `discussion_lessons` and call "
            "the configured embeddings provider in batches. Idempotent "
            "+ resumable — re-running picks up wherever the last run "
            "left off."
        ),
    )
    p.add_argument(
        "--owner-user-id", type=_parse_uuid, default=None,
        help="Scope to a single owner UUID. Omit to backfill every owner.",
    )
    p.add_argument(
        "--market", type=str, default=None,
        help="Scope to one market (TW / US / GLOBAL). Omit to backfill all.",
    )
    p.add_argument(
        "--max-rows", type=int, default=None,
        help=(
            "Stop after processing this many candidate rows. None "
            "(default) processes every pending row in one run."
        ),
    )
    p.add_argument(
        "--log-level", type=str, default="INFO",
        help="Python logging level (DEBUG / INFO / WARNING / ERROR).",
    )
    return p


if __name__ == "__main__":
    parser = _build_argparser()
    ns = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, ns.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(asyncio.run(_main(ns)))
