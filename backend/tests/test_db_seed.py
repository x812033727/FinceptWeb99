from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import IntegrityError

from db.seed import seed_admin


@pytest.mark.asyncio
async def test_seed_admin_recovers_when_another_worker_wins_insert(monkeypatch):
    monkeypatch.setattr("db.seed.settings.ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr("db.seed.settings.ADMIN_PASSWORD", "AdminPass99!")

    db = Mock()
    # First lookup sees no user; the lookup after the unique violation sees
    # the row committed by the other worker.
    db.scalar = AsyncMock(side_effect=[None, object()])
    db.commit = AsyncMock(
        side_effect=IntegrityError("insert", {}, Exception("duplicate email"))
    )
    db.rollback = AsyncMock()

    await seed_admin(db)

    db.add.assert_called_once()
    db.rollback.assert_awaited_once()
    assert db.scalar.await_count == 2


@pytest.mark.asyncio
async def test_seed_admin_reraises_unrelated_integrity_error(monkeypatch):
    monkeypatch.setattr("db.seed.settings.ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr("db.seed.settings.ADMIN_PASSWORD", "AdminPass99!")

    error = IntegrityError("insert", {}, Exception("unrelated constraint"))
    db = Mock()
    db.scalar = AsyncMock(side_effect=[None, None])
    db.commit = AsyncMock(side_effect=error)
    db.rollback = AsyncMock()

    with pytest.raises(IntegrityError) as caught:
        await seed_admin(db)

    assert caught.value is error
    db.rollback.assert_awaited_once()
