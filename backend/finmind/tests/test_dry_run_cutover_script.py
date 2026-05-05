"""Tests for `finmind.scripts.dry_run_cutover` — pre-flip diff between
FinMind and the candidate self-crawl source.

Pins:
    - exit 0 when row-count delta within tolerance
    - exit 1 when self-crawl errors out
    - --all enumerates only datasets whose fallback has a wired handler
    - tolerance arg flows through to status computation
"""
from __future__ import annotations

import sys

import pytest

from finmind.scripts.dry_run_cutover import (
    DiffOutcome,
    _enumerate_all_datasets,
)


def test_diff_outcome_status_ok_within_tolerance():
    o = DiffOutcome(
        dataset="TaiwanStockPrice", source="twse",
        finmind_rows=20, selfcrawl_rows=20,
        finmind_error=None, selfcrawl_error=None,
        delta_pct=0.0, note="",
    )
    assert o.status(tolerance_pct=5.0) == "OK"


def test_diff_outcome_status_warn_when_delta_exceeds_tolerance():
    """20 vs 18 = -10% delta. Tolerance 5% → WARN. Tolerance 15% → OK."""
    o = DiffOutcome(
        dataset="TaiwanStockPrice", source="twse",
        finmind_rows=20, selfcrawl_rows=18,
        finmind_error=None, selfcrawl_error=None,
        delta_pct=-10.0, note="",
    )
    assert o.status(tolerance_pct=5.0) == "WARN"
    assert o.status(tolerance_pct=15.0) == "OK"


def test_diff_outcome_status_fail_only_on_selfcrawl_error():
    """Self-crawl side errored → FAIL (operator MUST fix). FinMind
    side errored but self-crawl works → WARN (informational; the
    whole point of cutover is FinMind goes away)."""
    fail = DiffOutcome(
        dataset="X", source="twse",
        finmind_rows=10, selfcrawl_rows=None,
        finmind_error=None, selfcrawl_error="HTTPError: 503",
        delta_pct=None, note="",
    )
    assert fail.status(tolerance_pct=5.0) == "FAIL"

    warn = DiffOutcome(
        dataset="X", source="twse",
        finmind_rows=None, selfcrawl_rows=10,
        finmind_error="paywall", selfcrawl_error=None,
        delta_pct=None, note="",
    )
    assert warn.status(tolerance_pct=5.0) == "WARN"


def test_enumerate_all_datasets_skips_stub_sources():
    """`--all` mode should only diff datasets whose fallback source
    has a real handler. tpex/taifex/tdcc currently stubbed → their
    datasets must NOT show up in the enumeration."""
    targets = _enumerate_all_datasets()
    sources = {src for _, src in targets}
    assert "tpex" not in sources
    assert "taifex" not in sources
    assert "tdcc" not in sources
    # Headline TWSE handler must show up.
    assert ("TaiwanStockPrice", "twse") in targets
    # Headline MOPS handler must show up.
    assert ("TaiwanStockMonthRevenue", "mops") in targets


@pytest.mark.asyncio
async def test_cli_single_dataset_happy_path(monkeypatch, capsys):
    """Both sides return 10 rows → exit 0, OK row in markdown."""
    counts = {"finmind": 10, "twse": 10}

    def fake_resolve(source: str):
        class C:
            async def fetch(self, *a, **k):
                return [{}] * counts[source]
        return C()

    monkeypatch.setattr(
        "finmind.ingest.selfcrawl.resolve_client", fake_resolve,
    )
    monkeypatch.setattr(sys, "argv", [
        "dry_run_cutover",
        "--dataset", "TaiwanStockPrice", "--source", "twse",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.dry_run_cutover import amain

    rc = await amain()
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert "TaiwanStockPrice" in out


@pytest.mark.asyncio
async def test_cli_selfcrawl_error_exits_one(monkeypatch, capsys):
    """Self-crawl side raises → status FAIL, exit 1."""
    def fake_resolve(source: str):
        class C:
            async def fetch(self, *a, **k):
                if source == "finmind":
                    return [{}] * 10
                raise RuntimeError("simulated TWSE outage")
        return C()

    monkeypatch.setattr(
        "finmind.ingest.selfcrawl.resolve_client", fake_resolve,
    )
    monkeypatch.setattr(sys, "argv", [
        "dry_run_cutover",
        "--dataset", "TaiwanStockPrice", "--source", "twse",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
    ])
    from finmind.scripts.dry_run_cutover import amain

    rc = await amain()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "RuntimeError" in out


@pytest.mark.asyncio
async def test_cli_tolerance_flag_changes_pass_fail(monkeypatch, capsys):
    """20 vs 18 rows = -10% delta. Default tolerance 5% → exit 1.
    Bumped tolerance 15% → exit 0. Same data, different verdict."""
    def fake_resolve(source: str):
        class C:
            async def fetch(self, *a, **k):
                return [{}] * (20 if source == "finmind" else 18)
        return C()

    monkeypatch.setattr(
        "finmind.ingest.selfcrawl.resolve_client", fake_resolve,
    )
    monkeypatch.setattr(sys, "argv", [
        "dry_run_cutover",
        "--dataset", "TaiwanStockPrice", "--source", "twse",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
        "--tolerance", "5",
    ])
    from finmind.scripts.dry_run_cutover import amain

    assert await amain() == 1
    capsys.readouterr()  # Drain.

    monkeypatch.setattr(sys, "argv", [
        "dry_run_cutover",
        "--dataset", "TaiwanStockPrice", "--source", "twse",
        "--symbol", "2330",
        "--start", "2024-01-01", "--end", "2024-01-31",
        "--tolerance", "15",
    ])
    assert await amain() == 0
