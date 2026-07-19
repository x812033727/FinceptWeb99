"""CLI wiring tests for the due-dataset scheduler."""
from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _outcome(
    dataset_code: str,
    *,
    status: str = "done",
    rows_written: int = 0,
    error: str | None = None,
):
    return SimpleNamespace(
        chunk=SimpleNamespace(
            dataset_code=dataset_code,
            symbol=None,
            range_start=date(2026, 7, 17),
            range_end=date(2026, 7, 17),
        ),
        result=SimpleNamespace(
            status=status,
            rows_written=rows_written,
            error=error,
        ),
    )


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
    record_health = AsyncMock()
    monkeypatch.setattr(run_due, "record_health", record_health)
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
    record_health.assert_awaited_once_with(
        "finmind_tw_marketwide",
        ok=True,
        row_count=0,
        error=None,
    )


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


@pytest.mark.asyncio
async def test_tw_only_success_records_total_rows(monkeypatch):
    from finmind.scripts import run_due

    async def fake_run_due_now(_session, **_kwargs):
        return [
            _outcome("TaiwanStockPrice", rows_written=12),
            _outcome("taiwan_futures_daily", rows_written=7),
        ]

    record_health = AsyncMock()
    monkeypatch.setattr(run_due, "run_due_now", fake_run_due_now)
    monkeypatch.setattr(run_due, "record_health", record_health)
    monkeypatch.setattr(sys, "argv", ["run_due", "--tw-only"])

    rc = await run_due.amain()

    assert rc == run_due.EXIT_OK
    record_health.assert_awaited_once_with(
        "finmind_tw_marketwide",
        ok=True,
        row_count=19,
        error=None,
    )


@pytest.mark.asyncio
async def test_tw_only_partial_failure_is_redacted_and_bounded(
    monkeypatch, capsys,
):
    from finmind.scripts import run_due

    outcomes = [
        _outcome("TaiwanGood", rows_written=8),
        *[
            _outcome(
                f"TaiwanFailed{index}",
                status="failed",
                error=(
                    "GET https://api.finmindtrade.com/api/v4/data?"
                    "token=super-secret-token&dataset=TaiwanFailed"
                ),
            )
            for index in range(1, 7)
        ],
    ]

    async def fake_run_due_now(_session, **_kwargs):
        return outcomes

    record_health = AsyncMock()
    monkeypatch.setattr(run_due, "run_due_now", fake_run_due_now)
    monkeypatch.setattr(run_due, "record_health", record_health)
    monkeypatch.setattr(sys, "argv", ["run_due", "--tw-only"])

    rc = await run_due.amain()

    assert rc == run_due.EXIT_CHUNK_FAILURE
    kwargs = record_health.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 8
    assert "TaiwanFailed5" in kwargs["error"]
    assert "TaiwanFailed6" not in kwargs["error"]
    assert "(+1 more)" in kwargs["error"]
    assert "super-secret-token" not in kwargs["error"]
    assert "<redacted>" in kwargs["error"]
    assert "super-secret-token" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_tw_only_runner_crash_records_failure_and_returns_two(
    monkeypatch, capsys,
):
    from finmind.scripts import run_due

    async def crash(_session, **_kwargs):
        raise RuntimeError("database URL password=do-not-log")

    record_health = AsyncMock()
    monkeypatch.setattr(run_due, "run_due_now", crash)
    monkeypatch.setattr(run_due, "record_health", record_health)
    monkeypatch.setattr(sys, "argv", ["run_due", "--tw-only"])

    rc = await run_due.amain()

    assert rc == run_due.EXIT_RUNNER_CRASH
    kwargs = record_health.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["row_count"] == 0
    assert kwargs["error"].startswith("runner crash:")
    assert "do-not-log" not in kwargs["error"]
    assert "<redacted>" in kwargs["error"]
    assert "do-not-log" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_crypto_partial_failure_returns_one_without_tw_health(monkeypatch):
    from finmind.scripts import run_due

    async def fake_run_due_now(_session, **_kwargs):
        return [_outcome("CryptoPrice", status="failed", error="upstream down")]

    record_health = AsyncMock()
    monkeypatch.setattr(run_due, "run_due_now", fake_run_due_now)
    monkeypatch.setattr(run_due, "record_health", record_health)
    monkeypatch.setattr(run_due, "_load_crypto_symbols", AsyncMock(return_value=[]))
    monkeypatch.setattr(sys, "argv", [
        "run_due",
        "--crypto-universe-from-db",
    ])

    rc = await run_due.amain()

    assert rc == run_due.EXIT_CHUNK_FAILURE
    record_health.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_write_failure_does_not_mask_chunk_exit_code(monkeypatch):
    from finmind.scripts import run_due

    async def fake_run_due_now(_session, **_kwargs):
        return [_outcome("TaiwanFailed", status="failed", error="upstream down")]

    monkeypatch.setattr(run_due, "run_due_now", fake_run_due_now)
    monkeypatch.setattr(
        run_due,
        "record_health",
        AsyncMock(side_effect=RuntimeError("Redis password=health-secret")),
    )
    monkeypatch.setattr(sys, "argv", ["run_due", "--tw-only"])

    rc = await run_due.amain()

    assert rc == run_due.EXIT_CHUNK_FAILURE
