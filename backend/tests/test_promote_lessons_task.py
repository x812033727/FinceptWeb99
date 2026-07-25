from unittest.mock import AsyncMock, patch

import pytest

from tasks import promote_lessons


@pytest.mark.asyncio
async def test_run_promotes_every_market():
    with patch.object(promote_lessons, "promote_eligible_lessons",
                      new=AsyncMock(return_value=[])) as promote, \
         patch.object(promote_lessons, "AsyncSessionLocal") as sess:
        sess.return_value.__aenter__.return_value = AsyncMock()
        await promote_lessons.run()
    called_markets = [c.kwargs["market"] for c in promote.await_args_list]
    assert called_markets == list(promote_lessons.LESSON_MARKETS)
