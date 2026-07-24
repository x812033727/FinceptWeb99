from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from services import post_mortem_service


@pytest.mark.asyncio
async def test_no_conclusion_is_a_silent_noop():
    db = AsyncMock()
    disc = SimpleNamespace(id=uuid4(), conclusion=None, market="TW")
    db.refresh = AsyncMock()
    with patch.object(post_mortem_service, "build_post_mortem_message", new=AsyncMock()) as build:
        await post_mortem_service.run_post_mortem_pass(db, disc, uuid4())
    build.assert_not_awaited()
