"""Tests for scripts.backfill_verdicts — Discussion.verdict reclassifier.

The new 4-band classifier landed after the legacy verifier had
already written verdicts to many discussions, leaving stale "win"
values on rows whose closing prices actually crashed. The backfill
script walks every row, reclassifies via the new rule, and
overwrites where the band changes. These tests pin the script's
behavior on a small set of synthetic rows.
"""
from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.user import User, UserRole


@pytest.fixture(autouse=True)
async def _isolate_db(db_session: AsyncSession):
    await db_session.execute(delete(DiscussionTurn))
    await db_session.execute(delete(Discussion))
    await db_session.execute(delete(User))
    await db_session.commit()
    yield


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email="backfill_owner@example.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(u)
    await db_session.commit()
    return u


async def _disc(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    verdict: str | None,
    day1_open_prices: dict[str, float] | None,
    daily_close_prices: dict[str, list[float | None]] | None,
    recommended_symbols: list[str] | None = None,
) -> Discussion:
    now = datetime.now(UTC)
    syms = recommended_symbols or list((day1_open_prices or {}).keys())
    d = Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
        status="done", current_round=1,
        conclusion={
            "recommended_symbols": syms,
            "reasoning": "x", "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.5,
        },
        verdict=verdict,
        verdict_reason="legacy" if verdict else None,
        day1_open_prices=day1_open_prices,
        daily_close_prices=daily_close_prices,
        created_at=now, updated_at=now,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@pytest.fixture
def _patch_session(db_session: AsyncSession, monkeypatch):
    """Make backfill_verdicts._run() use the test DB session."""
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "scripts.backfill_verdicts.AsyncSessionLocal",
        lambda: _CM(),
    )


@pytest.mark.asyncio
async def test_relabels_legacy_win_to_big_loss_when_closes_crashed(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """Screenshot regression: pre-cutover row with verdict='win' but
    every close ≤ -5% must reclassify to 'big_loss' (大敗優先)."""
    from scripts import backfill_verdicts

    d = await _disc(
        db_session, owner.id,
        verdict="win",   # legacy stale value
        day1_open_prices={"2337": 100.0, "3044": 100.0},
        daily_close_prices={
            "2337": [98.3, 94.5, 93.0, 83.7, 82.0],     # trough -18%
            "3044": [95.9, 89.4, 87.8, 86.6, 86.6],     # trough -13.4%
        },
    )

    args = argparse.Namespace(dry_run=False, limit=None, log_level="INFO")
    changed = await backfill_verdicts._run(args)
    assert changed == 1

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "big_loss"
    assert "trough close" in (refreshed.verdict_reason or "")
    assert "[backfilled]" in (refreshed.verdict_reason or "")


@pytest.mark.asyncio
async def test_dry_run_does_not_commit(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """--dry-run reports the change count but leaves the DB intact."""
    from scripts import backfill_verdicts

    d = await _disc(
        db_session, owner.id,
        verdict="win",
        day1_open_prices={"2337": 100.0},
        daily_close_prices={"2337": [98.3, 94.5, 93.0, 83.7, 82.0]},
    )

    args = argparse.Namespace(dry_run=True, limit=None, log_level="INFO")
    changed = await backfill_verdicts._run(args)
    assert changed == 1

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    # Verdict unchanged in --dry-run
    assert refreshed.verdict == "win"
    assert refreshed.verdict_reason == "legacy"


@pytest.mark.asyncio
async def test_skips_rows_without_daily_close_prices(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """Rows missing daily_close_prices can't be reclassified — skip."""
    from scripts import backfill_verdicts

    await _disc(
        db_session, owner.id,
        verdict="win",
        day1_open_prices={"2330": 600.0},
        daily_close_prices=None,
    )

    args = argparse.Namespace(dry_run=False, limit=None, log_level="INFO")
    changed = await backfill_verdicts._run(args)
    assert changed == 0


@pytest.mark.asyncio
async def test_leaves_unverifiable_rows_alone(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """`unverifiable` discussions had no symbols / stuck data; the
    backfill must NOT re-grade them."""
    from scripts import backfill_verdicts

    d = await _disc(
        db_session, owner.id,
        verdict="unverifiable",
        # Even with usable data, unverifiable verdict is preserved
        day1_open_prices={"2330": 100.0},
        daily_close_prices={"2330": [105.0, 110.0, 115.0, 118.0, 122.0]},
    )

    args = argparse.Namespace(dry_run=False, limit=None, log_level="INFO")
    changed = await backfill_verdicts._run(args)
    assert changed == 0

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "unverifiable"


@pytest.mark.asyncio
async def test_fills_in_null_verdict_when_data_is_now_complete(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """Rows that were stuck deferred (verdict=NULL waiting for bars)
    but whose daily_close_prices is now populated should get a fresh
    verdict on the backfill pass."""
    from scripts import backfill_verdicts

    d = await _disc(
        db_session, owner.id,
        verdict=None,
        day1_open_prices={"2330": 100.0},
        daily_close_prices={"2330": [105.0, 110.0, 115.0, 118.0, 122.0]},
    )

    args = argparse.Namespace(dry_run=False, limit=None, log_level="INFO")
    changed = await backfill_verdicts._run(args)
    assert changed == 1

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "big_win"   # D5 +22%


@pytest.mark.asyncio
async def test_idempotent_on_second_run(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """Re-running the backfill on already-corrected rows must produce
    zero further changes."""
    from scripts import backfill_verdicts

    await _disc(
        db_session, owner.id,
        verdict="win",
        day1_open_prices={"2337": 100.0},
        daily_close_prices={"2337": [98.3, 94.5, 93.0, 83.7, 82.0]},
    )

    args = argparse.Namespace(dry_run=False, limit=None, log_level="INFO")
    first = await backfill_verdicts._run(args)
    assert first == 1
    second = await backfill_verdicts._run(args)
    assert second == 0


@pytest.mark.asyncio
async def test_limit_caps_processing(
    db_session: AsyncSession, owner: User, _patch_session,
):
    """--limit N caps the SELECT, so changes can't exceed N."""
    from scripts import backfill_verdicts

    for _ in range(3):
        await _disc(
            db_session, owner.id,
            verdict="win",
            day1_open_prices={"2337": 100.0},
            daily_close_prices={"2337": [98.3, 94.5, 93.0, 83.7, 82.0]},
        )

    args = argparse.Namespace(dry_run=False, limit=2, log_level="INFO")
    changed = await backfill_verdicts._run(args)
    assert changed == 2

    # The third row stays untouched
    remaining = (await db_session.scalars(
        select(Discussion).where(Discussion.verdict == "win"),
    )).all()
    assert len(remaining) == 1
