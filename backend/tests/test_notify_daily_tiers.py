"""The daily-ready notification names the tier split — presentation
only, computed from stored conclusions via `tier_for` (single source
of truth; no change to picking)."""
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

import tasks.auto_run_discussion as ar


@pytest.mark.asyncio
async def test_notify_names_recommend_and_watch_counts():
    with patch.object(ar, "notify_user", AsyncMock()) as notify:
        await ar._notify_daily_ready(
            "user-1", date(2026, 7, 28), {"price_signal": 1, "general": 2},
            tier_counts={"recommend": 1, "watch": 2},
        )
    notify.assert_awaited_once()
    payload = notify.await_args.args[1]
    assert "推薦 1" in payload["message"]
    assert "觀察名單 2" in payload["message"]
    assert payload["tier_counts"] == {"recommend": 1, "watch": 2}


@pytest.mark.asyncio
async def test_notify_without_tiers_keeps_legacy_message():
    """All-abstain days (and legacy callers) carry no tier fragment —
    an empty split must not render 「推薦 0 檔次」noise."""
    with patch.object(ar, "notify_user", AsyncMock()) as notify:
        await ar._notify_daily_ready(
            "user-1", date(2026, 7, 28), {"general": 1}, tier_counts={},
        )
    payload = notify.await_args.args[1]
    assert "推薦" not in payload["message"]
    assert "觀察名單" not in payload["message"]
    assert payload["message"].endswith("歡迎查看每日精選。")


@pytest.mark.asyncio
async def test_notify_default_signature_stays_compatible():
    with patch.object(ar, "notify_user", AsyncMock()) as notify:
        await ar._notify_daily_ready("user-1", date(2026, 7, 28), {"general": 1})
    payload = notify.await_args.args[1]
    assert "推薦" not in payload["message"]


@pytest.mark.asyncio
async def test_notify_failure_never_raises():
    with patch.object(ar, "notify_user", AsyncMock(side_effect=RuntimeError)):
        await ar._notify_daily_ready(
            "user-1", date(2026, 7, 28), {"general": 1},
            tier_counts={"recommend": 1},
        )
