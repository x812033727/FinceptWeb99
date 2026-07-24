"""Experiment knobs on the replay path.

Testing a hypothesis about ONE strategy's behaviour (e.g. relaxing
price_signal's macro veto) needs two things the replay path lacked: run a
single strategy instead of all three, and hand the panel a different rules
text without mutating the saved auto-run config the daily cron reads.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tasks import auto_run_discussion as A


def _cfg():
    return SimpleNamespace(
        user_id=uuid4(),
        topic="base topic",
        rules="base rules",
        strategy_run_counts={"general": 1, "chip_quality": 1, "price_signal": 1},
    )


@pytest.mark.asyncio
async def test_only_strategy_runs_that_strategy_alone():
    """`only_strategy` narrows the run to one strategy key."""
    counts = A.discussion_auto_run_config_service.normalize_strategy_run_counts(
        {"general": 1, "chip_quality": 1, "price_signal": 1}, legacy_enabled=True,
    )
    kept = A._select_strategy_counts(counts, only_strategy="price_signal")
    assert list(kept) == ["price_signal"]
    assert kept["price_signal"] >= 1


@pytest.mark.asyncio
async def test_no_only_strategy_keeps_every_strategy():
    counts = A.discussion_auto_run_config_service.normalize_strategy_run_counts(
        {"general": 1, "chip_quality": 1, "price_signal": 1}, legacy_enabled=True,
    )
    kept = A._select_strategy_counts(counts, only_strategy=None)
    assert set(kept) == set(counts)


@pytest.mark.asyncio
async def test_unknown_only_strategy_selects_nothing():
    """A typo must produce an empty run, not silently run everything."""
    counts = A.discussion_auto_run_config_service.normalize_strategy_run_counts(
        {"general": 1}, legacy_enabled=True,
    )
    assert A._select_strategy_counts(counts, only_strategy="typo") == {}


def test_rules_override_replaces_config_rules_without_mutating_it():
    cfg = _cfg()
    assert A._effective_rules(cfg, None) == "base rules"
    assert A._effective_rules(cfg, "relaxed veto rules") == "relaxed veto rules"
    # The saved config the daily cron reads is untouched.
    assert cfg.rules == "base rules"
