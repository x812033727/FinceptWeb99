from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tasks import verify_discussion_outcome as V


def _disc(verdict):
    return SimpleNamespace(id=uuid4(), owner_id=uuid4(), verdict=verdict,
                           market="TW", conclusion={"x": 1})


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,should_run", [
    ("win", True), ("big_loss", True), ("loss", True),
    ("abstain", False), ("unverifiable", False), (None, False),
])
async def test_live_post_mortem_only_on_decided(verdict, should_run):
    db = AsyncMock()
    with patch.object(V, "run_post_mortem_pass", new=AsyncMock()) as pm:
        await V.maybe_run_live_post_mortem(db, _disc(verdict))
    assert pm.await_count == (1 if should_run else 0)


@pytest.mark.asyncio
async def test_live_post_mortem_is_fail_closed():
    db = AsyncMock()
    with patch.object(V, "run_post_mortem_pass", new=AsyncMock(side_effect=RuntimeError("boom"))):
        # Must not raise.
        await V.maybe_run_live_post_mortem(db, _disc("win"))
