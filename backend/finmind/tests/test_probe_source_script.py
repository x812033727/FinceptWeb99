"""Tests for `finmind.scripts.probe_source` — CLI for probing self-
crawl connectors without writing to the DB.

The script is the standard "ready-to-merge" check before opening a
flip PR, so its exit codes and per-row summary are part of the
contract:
    0 = fetched ≥ 1 row
    1 = handler raised / unsupported (source, dataset)
    2 = handler returned empty list
"""
from __future__ import annotations

import sys

import pytest


@pytest.mark.asyncio
async def test_probe_unsupported_dataset_exits_one(monkeypatch, capsys):
    """source=twse covers 6 datasets — TaiwanFuturesDaily is not one
    of them. Probe must refuse with exit 1 and a remediation hint."""
    monkeypatch.setattr(sys, "argv", [
        "probe_source",
        "--dataset", "TaiwanFuturesDaily",
        "--source", "twse",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.probe_source import amain

    rc = await amain()
    assert rc == 1
    err = capsys.readouterr().err
    assert "no handler for dataset" in err
    assert "TaiwanFuturesDaily" in err


@pytest.mark.asyncio
async def test_probe_default_source_uses_catalog_fallback(monkeypatch, capsys):
    """No --source on the CLI → the script must look up the catalog
    fallback. TaiwanStockPrice's fallback is 'twse'."""
    captured: dict = {}

    class FakeClient:
        async def fetch(self, dataset, symbol, start, end):
            captured["dataset"] = dataset
            captured["symbol"] = symbol
            return [{"date": "2024-01-02", "stock_id": "2330", "close": 600.0}]

    def fake_resolve(source: str):
        captured["source"] = source
        return FakeClient()

    monkeypatch.setattr(
        "finmind.ingest.selfcrawl.resolve_client", fake_resolve,
    )
    monkeypatch.setattr(sys, "argv", [
        "probe_source",
        "--dataset", "TaiwanStockPrice",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.probe_source import amain

    rc = await amain()
    assert rc == 0
    assert captured["source"] == "twse"
    assert captured["dataset"] == "TaiwanStockPrice"
    assert captured["symbol"] == "2330"
    out = capsys.readouterr().out
    assert "fetched 1 row" in out


@pytest.mark.asyncio
async def test_probe_handler_returning_empty_exits_two(monkeypatch, capsys):
    """Empty rows ≠ exception, but the operator should still notice —
    exit code 2 distinguishes from real errors (exit 1)."""
    class EmptyClient:
        async def fetch(self, *_a, **_k):
            return []

    monkeypatch.setattr(
        "finmind.ingest.selfcrawl.resolve_client", lambda _src: EmptyClient(),
    )
    monkeypatch.setattr(sys, "argv", [
        "probe_source",
        "--dataset", "TaiwanStockPrice", "--source", "twse",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.probe_source import amain

    rc = await amain()
    assert rc == 2
    err = capsys.readouterr().err
    assert "0 rows" in err


@pytest.mark.asyncio
async def test_probe_handler_raising_exits_one(monkeypatch, capsys):
    """NotImplementedError + any other exception both exit 1; the
    error class shows up on stderr so CI logs are searchable."""
    class BoomClient:
        async def fetch(self, *_a, **_k):
            raise NotImplementedError("not wired yet")

    monkeypatch.setattr(
        "finmind.ingest.selfcrawl.resolve_client", lambda _src: BoomClient(),
    )
    monkeypatch.setattr(sys, "argv", [
        "probe_source",
        "--dataset", "TaiwanStockPrice", "--source", "twse",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.probe_source import amain

    rc = await amain()
    assert rc == 1
    err = capsys.readouterr().err
    assert "NotImplementedError" in err


@pytest.mark.asyncio
async def test_probe_dataset_with_no_fallback_requires_explicit_source(
    monkeypatch, capsys,
):
    """Datasets like `TaiwanStockNews` have `fallback_source=None` in
    the catalog (Google News RSS handles this in the main app) — the
    CLI must refuse rather than crash with a None argument."""
    monkeypatch.setattr(sys, "argv", [
        "probe_source",
        "--dataset", "TaiwanStockNews",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.probe_source import amain

    rc = await amain()
    assert rc == 1
    err = capsys.readouterr().err
    assert "no fallback_source" in err
