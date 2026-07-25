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


class _ScalarDB:
    """Minimal AsyncSession stand-in: `scalar` returns a canned value."""

    def __init__(self, value):
        self._value = value

    async def scalar(self, _stmt):
        return self._value


@pytest.mark.asyncio
async def test_experiment_sequence_lands_in_the_reserved_band():
    """First experiment run on a session must not collide with the
    production row that already holds slot 1."""
    offset = await A._experiment_sequence_offset(
        _ScalarDB(None), uuid4(), "2026-04-29", "price_signal",
    )
    assert offset + 1 == A.EXPERIMENT_SEQUENCE_BASE


@pytest.mark.asyncio
async def test_repeat_experiment_runs_do_not_collide_with_each_other():
    """A second run steps past the highest experiment sequence already
    stored, so re-running the same session is safe."""
    offset = await A._experiment_sequence_offset(
        _ScalarDB(A.EXPERIMENT_SEQUENCE_BASE), uuid4(), "2026-04-29", "price_signal",
    )
    assert offset + 1 == A.EXPERIMENT_SEQUENCE_BASE + 1


def test_experiment_rows_are_kept_out_of_the_learning_loop():
    """A row run under non-production rules must not produce lessons: they
    would be promoted into the semantic tier that steers the live panel."""
    from tasks.verify_discussion_outcome import is_experiment

    experiment = SimpleNamespace(
        candidate_snapshot={"strategy": "price_signal", "experiment": "rules_override"},
    )
    production = SimpleNamespace(
        candidate_snapshot={"strategy": "price_signal", "sequence": 1},
    )
    assert is_experiment(experiment) is True
    assert is_experiment(production) is False
    # A plain live row has no snapshot at all.
    assert is_experiment(SimpleNamespace(candidate_snapshot=None)) is False


@pytest.mark.asyncio
async def test_post_mortem_skips_experiment_rows():
    from tasks import verify_discussion_outcome as V

    calls = []

    async def _fake_pass(db, d, owner_id):
        calls.append(d)

    original = V.run_post_mortem_pass
    V.run_post_mortem_pass = _fake_pass
    try:
        experiment = SimpleNamespace(
            id=uuid4(), owner_id=uuid4(), verdict="win",
            candidate_snapshot={"experiment": "rules_override"},
        )
        production = SimpleNamespace(
            id=uuid4(), owner_id=uuid4(), verdict="win", candidate_snapshot={},
        )
        await V.maybe_run_live_post_mortem(None, experiment)
        assert calls == []
        await V.maybe_run_live_post_mortem(None, production)
        assert calls == [production]
    finally:
        V.run_post_mortem_pass = original


def test_rules_override_replaces_config_rules_without_mutating_it():
    cfg = _cfg()
    assert A._effective_rules(cfg, None) == "base rules"
    assert A._effective_rules(cfg, "relaxed veto rules") == "relaxed veto rules"
    # The saved config the daily cron reads is untouched.
    assert cfg.rules == "base rules"
