"""R6 PR2 round digest generation (services.discussion.round_digest).

Pure unit tests — `stream_chat` and `record_usage` are patched at the
`discussion_service` boundary, so no LLM and no DB are touched; the `db`
argument is a passthrough mock. Storage (`store_round_digest`) is a
one-line UPDATE exercised by the broader integration suite.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from models.discussion import DiscussionTurn
from services.discussion.round_digest import generate_round_digest


def _turn(persona_id: str, stance: str, content: str, rnd: int = 1) -> DiscussionTurn:
    return DiscussionTurn(
        discussion_id=uuid.uuid4(), round=rnd, turn_index=0,
        persona_id=persona_id, stance=stance, content=content,
    )


def test_digest_disabled_by_default():
    """The feature must ship OFF — enabling costs a model call per round."""
    assert settings.DISCUSSION_ROUND_DIGEST_ENABLED is False


@pytest.mark.asyncio
async def test_generate_summarizes_from_deltas():
    async def _fake_stream(*_a, **_k):
        for chunk in ["buffett 看多 2330;", "soros 看空。分歧未解。"]:
            yield {"type": "delta", "text": chunk}
        yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 20}

    turns = [
        _turn("buffett", "bullish", "2330 護城河深"),
        _turn("soros", "bearish", "評價過高"),
    ]
    with patch("services.discussion_service.stream_chat", _fake_stream), \
         patch("services.llm_usage_service.record_usage", AsyncMock()) as rec:
        out = await generate_round_digest(AsyncMock(), topic="2330", turns=turns)

    assert out == "buffett 看多 2330;soros 看空。分歧未解。"
    rec.assert_awaited_once()  # usage recorded for cost tracking


@pytest.mark.asyncio
async def test_generate_empty_input_skips_model_call():
    from services.discussion_service import USER_PERSONA_ID

    # A user-injection turn + a whitespace-only turn → no real content.
    turns = [
        _turn(USER_PERSONA_ID, "user_injection", "why?"),
        _turn("buffett", "bullish", "   "),
    ]
    stream = AsyncMock()
    with patch("services.discussion_service.stream_chat", stream):
        out = await generate_round_digest(AsyncMock(), topic="x", turns=turns)

    assert out is None
    stream.assert_not_called()


@pytest.mark.asyncio
async def test_generate_llm_error_returns_none():
    async def _err_stream(*_a, **_k):
        yield {"type": "error", "message": "boom"}

    turns = [_turn("buffett", "bullish", "有觀點")]
    with patch("services.discussion_service.stream_chat", _err_stream):
        out = await generate_round_digest(AsyncMock(), topic="x", turns=turns)

    assert out is None


@pytest.mark.asyncio
async def test_generate_blank_output_returns_none():
    async def _blank_stream(*_a, **_k):
        yield {"type": "delta", "text": "   \n  "}

    turns = [_turn("buffett", "bullish", "有觀點")]
    with patch("services.discussion_service.stream_chat", _blank_stream):
        out = await generate_round_digest(AsyncMock(), topic="x", turns=turns)

    assert out is None
