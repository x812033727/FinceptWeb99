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
    assert captured["dataset_code_prefixes"] is None
    assert captured["skip_per_symbol"] is False
    assert "nothing due" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_tw_only_market_wide_flags_reach_runner(
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
        "--tw-only",
        "--skip-per-symbol",
    ])

    rc = await run_due.amain()

    assert rc == 0
    assert captured["categories"] is None
    assert captured["dataset_code_prefixes"] == ("Taiwan", "taiwan_")
    assert captured["skip_per_symbol"] is True
    assert captured["crypto_symbols"] is None
    assert "nothing due" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_tw_only_rejects_crypto_scope(monkeypatch):
    from finmind.scripts import run_due

    monkeypatch.setattr(sys, "argv", [
        "run_due",
        "--tw-only",
        "--crypto-universe-from-db",
    ])

    with pytest.raises(SystemExit) as exc_info:
        await run_due.amain()

    assert exc_info.value.code == 2
