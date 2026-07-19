"""CLI wiring tests for the due-dataset scheduler."""
from __future__ import annotations

import sys

import pytest


@pytest.mark.asyncio
async def test_crypto_universe_flag_scopes_run_to_crypto(
    monkeypatch, capsys,
):
    from finmind.scripts import run_due

    captured: dict = {}

    async def fake_run_due_now(_session, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(run_due, "run_due_now", fake_run_due_now)
    monkeypatch.setattr(sys, "argv", [
        "run_due",
        "--crypto-universe-from-db",
    ])

    rc = await run_due.amain()

    assert rc == 0
    assert captured["categories"] == {"crypto"}
    assert captured["crypto_symbols"] == []
    assert "nothing due" in capsys.readouterr().out
