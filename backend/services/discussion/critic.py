"""R6 PR2 devil's-advocate critic.

Before the synthesiser commits to a conclusion, generate the strongest
case AGAINST the round's emerging consensus so the synthesiser has to
engage it rather than rubber-stamp groupthink. Off by default
(`DISCUSSION_SYNTH_CRITIC_ENABLED`) and best-effort — any failure returns
None and synthesis proceeds exactly as before.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings

log = logging.getLogger(__name__)

_CRITIC_SYSTEM = (
    "你是魔鬼代言人。讀完一場投資討論的逐字稿後,對『浮現中的共識』提出最強的反面"
    "論證(250 字以內繁體中文)。規則:①點出共識最可能錯的 2-3 個具體理由"
    "(資料盲點、被忽略的下行風險、過度外推、羊群效應等);②具體、可證偽,不要空話"
    "或『市場有風險』這種廢話;③不要虛構任何數字;④純文字,不要 JSON、不要標題。"
    "若這場討論分歧本來就很大、沒有明顯共識,直接說明『尚無明顯共識可供批判』。"
)


async def generate_devils_advocate(
    db: AsyncSession,
    *,
    topic: str,
    transcript: str,
    user_id: str | None = None,
    discussion_id: uuid.UUID | None = None,
) -> str | None:
    """Return the strongest counter-argument to `transcript`'s emerging
    consensus, or None on empty input / any model failure (best-effort)."""
    from services.discussion_service import stream_chat

    if not (transcript or "").strip():
        return None

    messages = [
        {"role": "system", "content": _CRITIC_SYSTEM},
        {
            "role": "user",
            "content": (
                f"討論主題:{topic}\n\n逐字稿:\n{transcript}\n\n請提出反方觀點:"
            ),
        },
    ]
    assembled = ""
    usage_seen: dict[str, int] | None = None
    try:
        async for event in stream_chat(
            messages=messages,
            provider="anthropic",
            model=settings.DISCUSSION_SYNTH_CRITIC_MODEL,
            max_tokens=settings.DISCUSSION_SYNTH_CRITIC_MAX_TOKENS,
            temperature=0.3,
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
                # `message` is a reserved LogRecord key — use `detail`.
                log.warning(
                    "discussion.critic.llm_error",
                    extra={"detail": event.get("message")},
                )
                return None
    except Exception:
        log.exception("discussion.critic.failed")
        return None

    if usage_seen is not None:
        try:
            from services.llm_usage_service import record_usage
            await record_usage(
                db,
                user_id=user_id,
                provider="anthropic",
                model=settings.DISCUSSION_SYNTH_CRITIC_MODEL,
                persona_id="_system:devils_advocate",
                prompt_tokens=usage_seen["prompt_tokens"],
                completion_tokens=usage_seen["completion_tokens"],
                discussion_id=discussion_id,
                round=None,
            )
        except Exception:
            log.warning("discussion.critic.usage_record_failed")

    return assembled.strip() or None
