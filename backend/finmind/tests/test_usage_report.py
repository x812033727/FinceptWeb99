"""Tests for `finmind.scripts.usage_report` — the FinMind upstream
per-dataset spend aggregator that drives cutover priority.

Redis is mocked (via `cache_hgetall`); the script has no DB dependency,
so these are pure aggregation-logic tests over injected day-hashes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from finmind.scripts import usage_report as ur


_TODAY = datetime(2026, 7, 12, tzinfo=timezone.utc)


def test_day_keys_are_newest_first_and_utc_stamped():
    keys = ur._day_keys(3, _TODAY)
    assert keys == ["20260712", "20260711", "20260710"]


def test_day_keys_floor_at_one_day():
    assert ur._day_keys(0, _TODAY) == ["20260712"]


@pytest.mark.asyncio
async def test_collect_usage_sums_across_days():
    """Same dataset seen on two different day-hashes accumulates; the
    per-day fan-out is summed into one total per dataset_code."""
    per_day = {
        "finmind:upstream:usage:20260712": {"TaiwanStockPrice": "10", "TaiwanStockPER": "3"},
        "finmind:upstream:usage:20260711": {"TaiwanStockPrice": "5"},
    }

    async def fake_hgetall(key):
        return per_day.get(key, {})

    with patch.object(ur, "cache_hgetall", new=AsyncMock(side_effect=fake_hgetall)):
        totals = await ur.collect_usage(2, _TODAY)

    assert totals == {"TaiwanStockPrice": 15, "TaiwanStockPER": 3}


@pytest.mark.asyncio
async def test_collect_usage_skips_malformed_values():
    with patch.object(
        ur, "cache_hgetall",
        new=AsyncMock(return_value={"GoodDataset": "4", "BadDataset": "oops"}),
    ):
        totals = await ur.collect_usage(1, _TODAY)
    assert totals == {"GoodDataset": 4}


def test_render_table_ranks_by_calls_descending():
    out = ur._render_table({"A": 3, "B": 30, "C": 7}, days=14)
    # B (highest) must appear before C before A in the ranked rows.
    assert out.index("| 1 | B |") < out.index("| 2 | C |") < out.index("| 3 | A |")
    assert "75.0%" in out  # B = 30 / 40


def test_render_table_empty_state():
    out = ur._render_table({}, days=7)
    assert "no usage recorded" in out
