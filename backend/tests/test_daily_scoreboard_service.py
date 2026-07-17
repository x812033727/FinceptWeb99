"""Per-strategy scoreboard aggregation over verified auto-run rows."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from models.discussion import Discussion
from models.user import User, UserRole
from services.daily_scoreboard_service import build_scoreboard


async def _owner(db):
    u = User(id=uuid.uuid4(), email=f"sb-{uuid.uuid4().hex[:8]}@example.com",
             hashed_password="x", role=UserRole.viewer, is_active=True)
    db.add(u)
    await db.flush()
    return u


def _verified(
    owner_id,
    *,
    strategy: str,
    verdict: str,
    day1: dict | None = None,
    day5: dict | None = None,
    pool_performance: dict | None = None,
    auto_run: bool = True,
    status: str = "done",
):
    now = datetime(2026, 7, 10, tzinfo=UTC)
    return Discussion(
        id=uuid.uuid4(), owner_id=owner_id, topic="t", rules="r",
        persona_ids=["buffett"], market="TW", status=status,
        current_round=5, conclusion={"reasoning": "x"},
        auto_run=auto_run, auto_run_strategy=strategy,
        verdict=verdict, day1_open_prices=day1, day5_close_prices=day5,
        pool_performance=pool_performance,
        created_at=now, updated_at=now,
    )


@pytest.mark.asyncio
async def test_scoreboard_groups_and_aggregates(db_session):
    owner = await _owner(db_session)
    db_session.add_all([
        # general: 2 wins (one big), 1 loss → win rate 2/3
        _verified(owner.id, strategy="general", verdict="big_win",
                  day1={"2330": 100.0}, day5={"2330": 125.0},
                  pool_performance={"avg_return_pct": 5.0, "resolved": 40, "pool_size": 50}),
        _verified(owner.id, strategy="general", verdict="win",
                  day1={"2330": 100.0}, day5={"2330": 105.0}),
        _verified(owner.id, strategy="general", verdict="loss",
                  day1={"2330": 100.0}, day5={"2330": 98.0}),
        # unverifiable is excluded from the win-rate denominator
        _verified(owner.id, strategy="general", verdict="unverifiable"),
        # retired key still aggregates under its own name
        _verified(owner.id, strategy="chip_momentum", verdict="win",
                  day1={"1101": 50.0}, day5={"1101": 55.0}),
        # non-auto-run and non-done rows are ignored
        _verified(owner.id, strategy="general", verdict="win", auto_run=False),
        _verified(owner.id, strategy="general", verdict="win", status="draft"),
    ])
    await db_session.commit()

    entries = await build_scoreboard(db_session, owner.id)
    by_key = {e["strategy"]: e for e in entries}

    g = by_key["general"]
    assert g["samples"] == 4
    assert g["wins"] == 2 and g["losses"] == 1
    assert g["big_wins"] == 1 and g["big_losses"] == 0
    assert g["unverifiable"] == 1
    assert g["win_rate"] == round(2 / 3, 4)
    # returns: +25%, +5%, -2% → mean 9.3333
    assert g["avg_return_pct"] == round((25 + 5 - 2) / 3, 4)
    # alpha only from the row with pool_performance: 25 - 5 = 20
    assert g["pool_samples"] == 1
    assert g["avg_alpha_pct"] == 20.0

    legacy = by_key["chip_momentum"]
    assert legacy["samples"] == 1 and legacy["win_rate"] == 1.0
    # ordered by sample count descending
    assert entries[0]["strategy"] == "general"


@pytest.mark.asyncio
async def test_scoreboard_empty_and_partial_prices(db_session):
    owner = await _owner(db_session)
    assert await build_scoreboard(db_session, owner.id) == []

    # verdict without prices: counted in win rate, absent from returns
    db_session.add(_verified(owner.id, strategy="price_signal", verdict="win"))
    await db_session.commit()
    entries = await build_scoreboard(db_session, owner.id)
    assert entries[0]["win_rate"] == 1.0
    assert entries[0]["avg_return_pct"] is None
    assert entries[0]["avg_alpha_pct"] is None
