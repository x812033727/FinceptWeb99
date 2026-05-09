"""Lesson semantic embedding (PR-J1).

The fetch ranking in `discussion_lesson_service.fetch_relevant_lessons`
mixes time-decay + symbol overlap + regime match + tier weight. Symbol
overlap is the only content-side signal and it's exact-string only,
so a lesson titled "半導體高估值風險" never matches a discussion that
names 2330 directly without happening to mention "半導體" too. PR-J1
fixes that by storing a semantic embedding alongside every lesson;
PR-J2 wires cosine similarity into the score so a query embedding
computed from `(topic, focus_symbols, market)` lifts the right
historical lesson regardless of vocabulary overlap.

This module is fail-soft by design — the discussion write path must
NEVER fail because the embedding API is down or unconfigured. Every
public function catches all exceptions and returns a falsy value
when something goes wrong, logging at warning level so ops can
still see degradation in metrics + logs.

Provider routing
----------------
Only OpenAI is wired up today (text-embedding-3-small at 1536
dimensions is the cost-effective default). Adding another provider
is one entry in `_PROVIDERS` plus an `_embed_via_<name>` function
returning `(vectors, model_used)`. API key resolution piggybacks on
the existing `llm_router._resolve_api_key` chain (per-user DB row →
system DB row → settings env), so admin-stored keys via the
LLMKeysCard work without a second integration.

Storage
-------
JSONB list-of-floats (no pgvector dep — see migration 0051). Cosine
similarity is computed in Python over the candidate window which is
already capped at 200 rows by the legacy fetch path; the math is
sub-millisecond at that scale.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion_lesson import DiscussionLesson

log = logging.getLogger(__name__)

# Per-call HTTP timeout for the embeddings provider. Embeddings are
# small + fast (~100ms typical for text-embedding-3-small); 15s leaves
# generous headroom for cold starts + transient network blips without
# letting a hung request hold the inline write path.
_EMBED_HTTP_TIMEOUT_S = 15.0

# Backfill batch size — OpenAI accepts up to 2048 inputs per call but
# the response payload size scales with `inputs × dims × 8 bytes` so
# we keep it at 32 to stay well under the JSON parse limits and to
# bound the blast radius of a single failed batch.
_BACKFILL_BATCH_SIZE = 32


# ── text builder ─────────────────────────────────────────────────────


def build_embed_text(
    lesson_text: str,
    *,
    market: str | None = None,
    category: str | None = None,
    related_symbols: Sequence[str] | None = None,
) -> str:
    """Format a lesson into the canonical embedding-input string.

    Mirroring this format on the query side (PR-J2) is what makes
    cosine similarity meaningful — both sides need to advertise
    `market` + `symbols` in the same shape so the embedding model
    learns to attend to them. Lesson side prepends the metadata,
    query side will do the same.

    Returns the empty string only when `lesson_text` is empty after
    strip — the caller treats that as "skip embed entirely".
    """
    text = (lesson_text or "").strip()
    if not text:
        return ""
    parts: list[str] = []
    meta_bits: list[str] = []
    if market:
        meta_bits.append(f"[market] {market}")
    if category:
        meta_bits.append(f"[category] {category}")
    syms = [s for s in (related_symbols or []) if s]
    if syms:
        meta_bits.append(f"[symbols] {','.join(syms[:10])}")
    if meta_bits:
        parts.append(" · ".join(meta_bits))
    parts.append(text)
    return "\n".join(parts)


# ── cosine + vector utilities ────────────────────────────────────────


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    """Cosine similarity over two equal-length float lists. Returns
    0.0 on any degenerate input (None, empty, mismatched length, all
    zeros). Operates on plain Python lists so it works against the
    JSONB-deserialized payload without a numpy dep.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


# ── provider dispatch ────────────────────────────────────────────────


async def _embed_via_openai(
    inputs: Sequence[str], *, model: str, api_key: str,
) -> list[list[float]] | None:
    """Call OpenAI /v1/embeddings with batched inputs. Returns the
    vectors aligned with `inputs` (same order, same length) on
    success, None on any failure. Fail-soft: every error path logs
    and returns None so the caller can decide whether to retry.
    """
    if not api_key:
        return None
    if not inputs:
        return []
    url = "https://api.openai.com/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {"model": model, "input": list(inputs)}
    try:
        async with httpx.AsyncClient(timeout=_EMBED_HTTP_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            log.warning(
                "lesson_embedding.openai_http_error",
                extra={
                    "status": resp.status_code,
                    "body_snippet": resp.text[:200],
                    "model": model,
                    "n_inputs": len(inputs),
                },
            )
            return None
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "lesson_embedding.openai_request_failed",
            extra={"error": str(exc), "model": model, "n_inputs": len(inputs)},
        )
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != len(inputs):
        log.warning(
            "lesson_embedding.openai_malformed_response",
            extra={"model": model, "n_inputs": len(inputs)},
        )
        return None
    out: list[list[float]] = []
    for entry in data:
        if not isinstance(entry, dict):
            return None
        vec = entry.get("embedding")
        if not isinstance(vec, list) or not vec:
            return None
        try:
            out.append([float(v) for v in vec])
        except (TypeError, ValueError):
            return None
    return out


# Provider table. Each entry is `(coroutine, default_model)`. Adding
# a new provider = wire its function here + ensure llm_router can
# resolve its key.
_PROviderCallable = Callable[..., Any]
_PROVIDERS: dict[str, tuple[_PROviderCallable, str]] = {
    "openai": (_embed_via_openai, "text-embedding-3-small"),
}


async def _resolve_provider_and_key(
    db: AsyncSession | None,
) -> tuple[str, str, str]:
    """Resolve `(provider, model, api_key)` for the embedding call.

    Two-tier resolution: env-supplied key first, then the admin-stored
    DB system row (`LLMProviderKey`). Intentionally does NOT route
    through `llm_router._resolve_api_key` because that helper's
    tier-4 admin-key fallback issues a JOIN against `users`, and that
    JOIN has been observed to leak result-set processor state into
    SQLAlchemy 2.0's cyextension row cache under aiosqlite +
    StaticPool — surfacing as the next test's `select(User)` reading
    User.id as a stray float (same flake class as PR f909d76's
    `'float' object has no attribute 'replace'`). Embedding is a
    background-only feature — no per-user customization is needed —
    so the simpler chain is sufficient AND eliminates the flake
    surface.

    Returns `("", "", "")` when the configured provider isn't
    supported yet — caller treats that as "skip embed".
    """
    provider = (
        getattr(settings, "LESSON_EMBEDDING_PROVIDER", "openai") or "openai"
    ).lower()
    if provider not in _PROVIDERS:
        log.warning(
            "lesson_embedding.unsupported_provider",
            extra={"provider": provider},
        )
        return "", "", ""
    model = getattr(settings, "LESSON_EMBEDDING_MODEL", "") or _PROVIDERS[provider][1]

    attr_map = {"openai": "OPENAI_API_KEY"}
    api_key = getattr(settings, attr_map.get(provider, ""), "") or ""

    # Tier 2 — admin-stored system row in `llm_provider_keys`. Single
    # `db.get(...)` lookup, no JOIN, no row-processor cross-talk.
    if not api_key and db is not None:
        try:
            from auth.llm_key_crypto import decrypt
            from models.llm_provider_key import LLMProviderKey
            row = await db.get(LLMProviderKey, provider)
            if row is not None:
                plain = decrypt(row.encrypted_key)
                if plain:
                    api_key = plain
        except Exception as exc:
            log.debug(
                "lesson_embedding.system_key_lookup_failed",
                extra={"provider": provider, "error": str(exc)},
            )
    return provider, model, api_key


# ── public API ───────────────────────────────────────────────────────


async def embed_texts(
    db: AsyncSession | None, texts: Sequence[str],
) -> tuple[list[list[float]] | None, str]:
    """Embed a batch of texts. Returns `(vectors, model_used)` on
    success or `(None, "")` on any failure (missing key, provider
    error, malformed response). The texts list is filtered to drop
    blank entries before the call; vectors are aligned with the
    INPUT list — caller is responsible for stripping blanks first
    if the alignment matters.
    """
    cleaned = [t for t in (texts or []) if isinstance(t, str) and t.strip()]
    if not cleaned:
        return [], ""
    provider, model, api_key = await _resolve_provider_and_key(db)
    if not provider or not api_key:
        return None, ""
    fn, _default_model = _PROVIDERS[provider]
    try:
        vectors = await fn(cleaned, model=model, api_key=api_key)
    except Exception as exc:
        log.warning(
            "lesson_embedding.provider_call_unexpected",
            extra={"provider": provider, "model": model, "error": str(exc)},
        )
        vectors = None
    if vectors is None:
        _bump_metric("failed")
        return None, ""
    _bump_metric("success", n=len(vectors))
    return vectors, model


async def embed_lesson(
    db: AsyncSession, lesson: DiscussionLesson,
) -> bool:
    """Compute + persist the embedding for a single lesson row.
    Returns True iff the row was updated, False on any failure
    (skipped, provider error, DB hiccup). Idempotent: a row that
    already has an embedding for the current model is skipped.
    """
    if not _enabled():
        return False
    if lesson is None:
        return False
    provider, model, api_key = await _resolve_provider_and_key(db)
    if not provider or not api_key:
        _bump_metric("skipped")
        return False
    if (
        lesson.embedding is not None
        and (lesson.embedding_model or "") == model
    ):
        return False

    text = build_embed_text(
        lesson.lesson_text,
        market=lesson.market,
        category=lesson.category,
        related_symbols=lesson.related_symbols,
    )
    if not text:
        return False
    vectors, model_used = await embed_texts(db, [text])
    if not vectors:
        return False

    lesson.embedding = vectors[0]
    lesson.embedding_model = model_used
    lesson.embedded_at = datetime.now(UTC)
    try:
        await db.commit()
    except Exception as exc:
        log.warning(
            "lesson_embedding.commit_failed",
            extra={"lesson_id": lesson.id, "error": str(exc)},
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return False
    return True


async def embed_lessons_bulk(
    db: AsyncSession, lessons: Sequence[DiscussionLesson],
) -> int:
    """Batch-embed a list of lesson rows in a single provider call.
    Returns the number of rows successfully updated. Skips rows that
    are already embedded with the current model. Used by the
    backfill script and by the inline write path when the
    synthesizer emits multiple lessons in one go.
    """
    if not _enabled() or not lessons:
        return 0
    provider, model, api_key = await _resolve_provider_and_key(db)
    if not provider or not api_key:
        _bump_metric("skipped", n=len(lessons))
        return 0

    pending: list[tuple[DiscussionLesson, str]] = []
    for lesson in lessons:
        if lesson is None:
            continue
        if (
            lesson.embedding is not None
            and (lesson.embedding_model or "") == model
        ):
            continue
        text = build_embed_text(
            lesson.lesson_text,
            market=lesson.market,
            category=lesson.category,
            related_symbols=lesson.related_symbols,
        )
        if text:
            pending.append((lesson, text))
    if not pending:
        return 0

    vectors, model_used = await embed_texts(db, [t for _row, t in pending])
    if not vectors or len(vectors) != len(pending):
        return 0

    now = datetime.now(UTC)
    for (row, _t), vec in zip(pending, vectors):
        row.embedding = vec
        row.embedding_model = model_used
        row.embedded_at = now
    try:
        await db.commit()
    except Exception as exc:
        log.warning(
            "lesson_embedding.bulk_commit_failed",
            extra={"n": len(pending), "error": str(exc)},
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return 0
    return len(pending)


async def backfill_pending(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID | None = None,
    market: str | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Walk un-embedded lessons (oldest first) and embed in batches.

    Args:
        owner_user_id: scope to one user's lessons; None = every owner.
        market: scope to one market; None = every market.
        max_rows: stop after processing this many candidate rows.
            None = process every pending row in one run.

    Returns:
        `{"scanned": int, "updated": int, "batches": int}`. Suitable
        for both the cron path and the manual `scripts/backfill_*.py`
        invocation.
    """
    if not _enabled():
        return {"scanned": 0, "updated": 0, "batches": 0, "skipped": True}

    conditions = [DiscussionLesson.embedding.is_(None)]
    if owner_user_id is not None:
        conditions.append(DiscussionLesson.owner_user_id == owner_user_id)
    if market:
        conditions.append(DiscussionLesson.market == market)

    scanned = 0
    updated = 0
    batches = 0
    while True:
        if max_rows is not None and scanned >= max_rows:
            break
        remaining = (
            None if max_rows is None else max(0, max_rows - scanned)
        )
        batch_limit = (
            _BACKFILL_BATCH_SIZE if remaining is None
            else min(_BACKFILL_BATCH_SIZE, remaining)
        )
        if batch_limit <= 0:
            break

        stmt = (
            select(DiscussionLesson)
            .where(*conditions)
            .order_by(DiscussionLesson.created_at.asc())
            .limit(batch_limit)
        )
        rows = list((await db.scalars(stmt)).all())
        if not rows:
            break
        scanned += len(rows)
        n_updated = await embed_lessons_bulk(db, rows)
        updated += n_updated
        batches += 1
        # If the bulk write failed (provider down, key missing), bail —
        # no point hammering the same broken provider for the rest of
        # the table.
        if n_updated == 0:
            break
    return {"scanned": scanned, "updated": updated, "batches": batches}


# ── helpers ──────────────────────────────────────────────────────────


def _enabled() -> bool:
    return bool(getattr(settings, "LESSON_EMBEDDING_ENABLED", True))


def _bump_metric(outcome: str, *, n: int = 1) -> None:
    try:
        from middleware.metrics import LESSON_EMBEDDINGS_TOTAL
        LESSON_EMBEDDINGS_TOTAL.labels(outcome=outcome).inc(n)
    except Exception:
        pass


__all__ = [
    "build_embed_text",
    "cosine",
    "embed_texts",
    "embed_lesson",
    "embed_lessons_bulk",
    "backfill_pending",
]
