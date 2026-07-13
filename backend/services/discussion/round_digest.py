"""R6 PR2 round digest — compress one round's debate into a compact
recap (unresolved disagreements preserved) via a cheap model, stored on
the round-context row.

Generation only: this does NOT change what personas see during a
discussion. It is off by default (`DISCUSSION_ROUND_DIGEST_ENABLED`) and
best-effort — any failure returns/stores None and the round proceeds
untouched. The token-saving consumption path (personas reading digests
instead of older transcripts) is a separate, validation-gated follow-up.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion import DiscussionTurn
from models.discussion_round_context import DiscussionRoundContext

log = logging.getLogger(__name__)

_DIGEST_SYSTEM = (
    "你是討論記錄員。把一輪多位投資專家的發言壓縮成 300 字以內的繁體中文摘要。"
    "規則:①逐位點名每位專家的立場(看多/看空/中立)與最關鍵的一個理由;"
    "②務必保留未解決的分歧(誰和誰對立、爭點是什麼);③不要下結論、不要加入"
    "自己的看法、不要虛構任何數字;④純文字,不要 JSON、不要 markdown 標題。"
)


async def generate_round_digest(
    db: AsyncSession,
    *,
    topic: str,
    turns: list[DiscussionTurn],
    user_id: str | None = None,
    discussion_id: uuid.UUID | None = None,
    round_number: int | None = None,
) -> str | None:
    """Summarise `turns` (one round) into a ≤300-word recap. Returns None
    on empty input or any model failure — best-effort, never raises."""
    from services.discussion_service import USER_PERSONA_ID, stream_chat

    real = [
        t for t in turns
        if t.persona_id != USER_PERSONA_ID and (t.content or "").strip()
    ]
    if not real:
        return None

    lines = [
        f"[{t.persona_id} · {t.stance}] {(t.content or '').strip()}"
        for t in real
    ]
    user_prompt = (
        f"討論主題:{topic}\n\n本輪發言:\n"
        + "\n\n".join(lines)
        + "\n\n請輸出摘要:"
    )
    messages = [
        {"role": "system", "content": _DIGEST_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    assembled = ""
    usage_seen: dict[str, int] | None = None
    try:
        async for event in stream_chat(
            messages=messages,
            provider="anthropic",
            model=settings.DISCUSSION_ROUND_DIGEST_MODEL,
            max_tokens=settings.DISCUSSION_ROUND_DIGEST_MAX_TOKENS,
            temperature=0.2,
            db=db,
            user_id=user_id,
        ):
            etype = event.get("type")
            if etype == "delta":
                assembled += event.get("text", "")
            elif etype == "usage":
                if usage_seen is None:
                    usage_seen = {"prompt_tokens": 0, "completion_tokens": 0}
                usage_seen["prompt_tokens"] += int(event.get("prompt_tokens", 0))
                usage_seen["completion_tokens"] += int(
                    event.get("completion_tokens", 0)
                )
            elif etype == "error":
                # NB: `message` is a reserved LogRecord attribute — putting
                # it in `extra` raises KeyError inside logging, so use a
                # distinct key.
                log.warning(
                    "discussion.round_digest.llm_error",
                    extra={"detail": event.get("message")},
                )
                return None
    except Exception:
        log.exception("discussion.round_digest.failed")
        return None

    if usage_seen is not None:
        try:
            from services.llm_usage_service import record_usage
            await record_usage(
                db,
                user_id=user_id,
                provider="anthropic",
                model=settings.DISCUSSION_ROUND_DIGEST_MODEL,
                persona_id="_system:round_digest",
                prompt_tokens=usage_seen["prompt_tokens"],
                completion_tokens=usage_seen["completion_tokens"],
                discussion_id=discussion_id,
                round=round_number,
            )
        except Exception:
            log.warning("discussion.round_digest.usage_record_failed")

    return assembled.strip() or None


async def store_round_digest(
    db: AsyncSession,
    *,
    discussion_id: uuid.UUID,
    round_number: int,
    digest: str,
) -> None:
    """Persist the digest onto the (discussion, round) context row. The
    row already exists (upserted at round start); this only sets `digest`.
    Best-effort — a missing row (round-context snapshot failed to persist)
    is a no-op, never an error."""
    await db.execute(
        update(DiscussionRoundContext)
        .where(
            DiscussionRoundContext.discussion_id == discussion_id,
            DiscussionRoundContext.round == round_number,
        )
        .values(digest=digest)
    )
    await db.commit()
