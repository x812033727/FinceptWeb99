"""Tests for `finmind.billing` — Phase 4 key issuance + verification.

Verifies:
  - issue_key returns a plaintext + persists prefix + hash
  - verify_key recovers the row from the plaintext
  - tampered plaintext returns None
  - disabled key returns None
  - expired key returns None
  - touch_last_used updates timestamp
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finmind.billing.keys import (
    KEY_PREFIX,
    PREFIX_LEN,
    issue_key,
    touch_last_used,
    verify_key,
)


@pytest.mark.asyncio
async def test_issue_key_returns_plaintext_and_persists_hash(finmind_session):
    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
        name="default",
    )

    assert issued.plaintext.startswith(KEY_PREFIX)
    assert len(issued.prefix) == PREFIX_LEN
    assert issued.prefix == issued.plaintext[:PREFIX_LEN]
    assert issued.record_id > 0


@pytest.mark.asyncio
async def test_verify_key_round_trips(finmind_session):
    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
    )

    row = await verify_key(finmind_session, issued.plaintext)
    assert row is not None
    assert row.id == issued.record_id
    assert row.owner_email == "user@example.com"


@pytest.mark.asyncio
async def test_verify_key_rejects_tampered_suffix(finmind_session):
    """Same prefix but different suffix — must not authenticate."""
    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
    )

    tampered = issued.plaintext[:-4] + "XXXX"
    row = await verify_key(finmind_session, tampered)
    assert row is None


@pytest.mark.asyncio
async def test_verify_key_rejects_unknown_prefix(finmind_session):
    row = await verify_key(finmind_session, f"{KEY_PREFIX}garbageXX_neverissued")
    assert row is None


@pytest.mark.asyncio
async def test_verify_key_rejects_disabled(finmind_session):
    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
    )

    # Disable the key.
    from finmind.models.billing import ApiKey

    record = await finmind_session.get(ApiKey, issued.record_id)
    record.enabled = False
    await finmind_session.commit()

    row = await verify_key(finmind_session, issued.plaintext)
    assert row is None


@pytest.mark.asyncio
async def test_verify_key_rejects_expired(finmind_session):
    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
    )

    from finmind.models.billing import ApiKey

    record = await finmind_session.get(ApiKey, issued.record_id)
    record.expires_at = datetime.now(tz=UTC) - timedelta(hours=1)
    await finmind_session.commit()

    row = await verify_key(finmind_session, issued.plaintext)
    assert row is None


@pytest.mark.asyncio
async def test_touch_last_used_records_timestamp(finmind_session):
    issued = await issue_key(
        finmind_session,
        owner_email="user@example.com",
    )

    from finmind.models.billing import ApiKey

    before_record = await finmind_session.get(ApiKey, issued.record_id)
    assert before_record.last_used_at is None

    await touch_last_used(finmind_session, issued.record_id)

    after_record = await finmind_session.get(ApiKey, issued.record_id)
    await finmind_session.refresh(after_record)
    assert after_record.last_used_at is not None
