"""Tests for scripts.apply_event_window_clause — the operator tool
that appends/removes the event-window handling clause on
`DiscussionAutoRunConfig.rules`. Mirrors the pure-function coverage of
`test_apply_veto_downgrade`; the archive-dir plumbing is shared with
that script and tested there.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from scripts.apply_event_window_clause import (
    EVENT_WINDOW_CLAUSE,
    apply_clause,
    main as ew_main,
    revert_clause,
)

_BASE = "1. 每位專家發言 ≤ 200 字。\n2. 必須引用至少一個具體數據。"


def test_apply_appends_once():
    once = apply_clause(_BASE)
    assert once == _BASE + EVENT_WINDOW_CLAUSE
    # Idempotent: applying again is a no-op.
    assert apply_clause(once) == once


def test_revert_restores_exactly():
    assert revert_clause(apply_clause(_BASE)) == _BASE
    # Reverting rules that never had the clause is a no-op.
    assert revert_clause(_BASE) == _BASE


def test_clause_wording_pins_the_review_findings():
    """The clause must address each observed failure mode from the
    2026-08 miss review: ex-dividend as an automatic veto, selective
    application across the batch, and invented risk-reward floors."""
    assert "除息" in EVENT_WINDOW_CLAUSE
    assert "一致適用" in EVENT_WINDOW_CLAUSE
    assert "風報比門檻" in EVENT_WINDOW_CLAUSE
    # It is an all-strategy clause, unlike the macro-veto downgrade.
    assert "適用所有策略場次" in EVENT_WINDOW_CLAUSE


# ── main() with a fully fake DB session (same shape as the veto
# script's tests — `AsyncSessionLocal` is imported inside main(), so
# the patch target is `db.session.AsyncSessionLocal`). ──────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, cfgs):
        self._cfgs = cfgs
        self.commit = AsyncMock()

    async def scalars(self, stmt):
        return _FakeResult(self._cfgs)


class _CM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_main_show_mode_mutates_nothing():
    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await ew_main("show", force_without_archive=False)

    assert cfg.rules == "原有規則。"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_apply_archives_and_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(tmp_path))
    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await ew_main("apply", force_without_archive=False)

    assert EVENT_WINDOW_CLAUSE in cfg.rules
    session.commit.assert_awaited_once()

    archived = list(tmp_path.glob("rules-u1-*.txt"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "原有規則。"


@pytest.mark.asyncio
async def test_main_revert_restores_original(tmp_path, monkeypatch):
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(tmp_path))
    cfg = SimpleNamespace(
        user_id="u1", rules="原有規則。" + EVENT_WINDOW_CLAUSE,
    )
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await ew_main("revert", force_without_archive=False)

    assert cfg.rules == "原有規則。"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_apply_archive_failure_without_force_skips_mutation(
    tmp_path, monkeypatch,
):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("VETO_ARCHIVE_DIR", str(blocker / "rules-archive"))

    cfg = SimpleNamespace(user_id="u1", rules="原有規則。")
    session = _FakeSession([cfg])
    with patch("db.session.AsyncSessionLocal", return_value=_CM(session)):
        await ew_main("apply", force_without_archive=False)

    assert cfg.rules == "原有規則。"
    session.commit.assert_not_awaited()
