"""R6 PR2 devil's-advocate critic (services.discussion.critic).

Pure unit tests — `stream_chat` / `record_usage` patched at the
`discussion_service` boundary, `db` is a passthrough mock.
"""
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from services.discussion.critic import generate_devils_advocate


def test_critic_disabled_by_default():
    assert settings.DISCUSSION_SYNTH_CRITIC_ENABLED is False


@pytest.mark.asyncio
async def test_generate_returns_counterargument_and_records_usage():
    async def _fake(*_a, **_k):
        yield {"type": "delta", "text": "共識忽略了升息風險;"}
        yield {"type": "delta", "text": "外資已連賣。"}
        yield {"type": "usage", "prompt_tokens": 200, "completion_tokens": 30}

    with patch("services.discussion_service.stream_chat", _fake), \
         patch("services.llm_usage_service.record_usage", AsyncMock()) as rec:
        out = await generate_devils_advocate(
            AsyncMock(), topic="2330", transcript="buffett: 看多\nsoros: 看多",
        )
    assert out == "共識忽略了升息風險;外資已連賣。"
    rec.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_empty_transcript_skips_model_call():
    stream = AsyncMock()
    with patch("services.discussion_service.stream_chat", stream):
        out = await generate_devils_advocate(AsyncMock(), topic="x", transcript="  ")
    assert out is None
    stream.assert_not_called()


@pytest.mark.asyncio
async def test_generate_llm_error_returns_none():
    async def _err(*_a, **_k):
        yield {"type": "error", "message": "boom"}

    with patch("services.discussion_service.stream_chat", _err):
        out = await generate_devils_advocate(AsyncMock(), topic="x", transcript="有內容")
    assert out is None
