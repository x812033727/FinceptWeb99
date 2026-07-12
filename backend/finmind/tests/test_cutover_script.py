"""Tests for `finmind.scripts.cutover` — the Phase A' routing flip that
switches Taiwan datasets from FinMind to direct self-crawl.

Pins:
    - the plan targets exactly the datasets whose fallback has a wired
      handler (== the AdminPage flip-gate accepted set)
    - --commit flips active_source + enables, and is idempotent
    - datasets not seeded / not covered are skipped, never flipped
    - a plan never proposes flipping a dataset its target can't serve
"""
from __future__ import annotations

import pytest

from finmind.dataset_catalog import all_entries
from finmind.ingest.selfcrawl import covers_dataset
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.cutover import (
    _eligible_targets,
    apply_cutover,
    plan_cutover,
)
from finmind.scripts.init_db import seed_dataset_sources


def _expected_covered() -> set[str]:
    return {
        e.dataset_code
        for _, e in all_entries()
        if e.fallback_source
        and e.local_table
        and covers_dataset(e.fallback_source, e.dataset_code)
    }


def test_eligible_targets_match_covered_set():
    """`_eligible_targets` must equal the covers_dataset-derived set so
    the cutover tool and the AdminPage flip gate never disagree about
    what is shippable."""
    got = {code for _, code, _, _ in _eligible_targets()}
    assert got == _expected_covered()
    # Sanity: every target's declared source really covers it.
    for _, code, source, table in _eligible_targets():
        assert covers_dataset(source, code)
        assert table  # never empty local_table


@pytest.mark.asyncio
async def test_plan_all_flips_when_seeded_on_finmind(finmind_session):
    await seed_dataset_sources()  # active_source seeds to primary=finmind

    items = await plan_cutover(finmind_session)
    flips = [i for i in items if i.action == "flip"]

    assert {i.dataset_code for i in flips} == _expected_covered()
    # Every flip moves off finmind onto its declared fallback and would
    # enable the row.
    for i in flips:
        assert i.current_source == "finmind"
        assert i.target_source != "finmind"
        assert covers_dataset(i.target_source, i.dataset_code)


@pytest.mark.asyncio
async def test_commit_flips_and_is_idempotent(finmind_session):
    await seed_dataset_sources()

    items = await plan_cutover(finmind_session)
    changed = await apply_cutover(finmind_session, items)
    await finmind_session.commit()
    assert changed == len(_expected_covered())

    # Spot-check a couple of rows landed on their fallback + enabled.
    price = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    assert price.active_source == "twse"
    assert price.enabled is True
    bond = await finmind_session.get(DatasetSource, "GovernmentBondsYield")
    assert bond.active_source == "fred"  # macro cutover, previously blocked
    assert bond.enabled is True

    # Re-planning now yields zero flips (all unchanged) — idempotent.
    items2 = await plan_cutover(finmind_session)
    assert [i for i in items2 if i.action == "flip"] == []
    assert {i.action for i in items2} <= {"unchanged", "skip"}


@pytest.mark.asyncio
async def test_source_filter_restricts_plan(finmind_session):
    await seed_dataset_sources()

    items = await plan_cutover(finmind_session, only_source="fred")
    assert {i.dataset_code for i in items} == {
        "GovernmentBondsYield",
        "CrudeOilPrices",
    }
    assert all(i.target_source == "fred" for i in items)


@pytest.mark.asyncio
async def test_missing_row_is_skipped_not_flipped(finmind_session):
    """A covered dataset that isn't seeded (init_db not run for it) shows
    up as skip/'not seeded', never a flip — the tool can't enable a row
    that doesn't exist."""
    await seed_dataset_sources()
    # Delete one seeded covered dataset to simulate an unseeded row.
    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    await finmind_session.delete(row)
    await finmind_session.commit()

    items = await plan_cutover(finmind_session, only_dataset="TaiwanStockPrice")
    assert len(items) == 1
    assert items[0].action == "skip"
    assert "not seeded" in items[0].reason
