"""Health-outcome semantics for the FinMind market-wide run.

Observed inversion (5 consecutive days in production): the run that
wrote 15k+ rows reported `failed` because one chunk of ~30 timed out,
while the runs that wrote nothing reported `ok` because nothing was
due. Any consumer gating on this signal is misled in both directions.
"""
from types import SimpleNamespace

from finmind.scripts.run_due import classify_run_outcome


def _outcome(status: str, rows: int, dataset: str = "ds", error: str | None = None):
    return SimpleNamespace(
        chunk=SimpleNamespace(dataset_code=dataset, symbol=None,
                              range_start="2026-07-01", range_end="2026-07-24"),
        result=SimpleNamespace(status=status, rows_written=rows, error=error),
    )


def test_idle_run_is_ok_and_labeled():
    ok, err = classify_run_outcome([])
    assert ok is True
    assert err == "idle: nothing due"


def test_clean_run_is_ok():
    ok, err = classify_run_outcome([_outcome("done", 500)])
    assert ok is True
    assert err is None


def test_partial_failure_with_rows_written_is_ok_with_summary():
    """One timed-out chunk must not label a 15k-row run as failed."""
    ok, err = classify_run_outcome([
        _outcome("done", 15_000),
        _outcome("failed", 0, dataset="TaiwanStockPER", error="timeout"),
    ])
    assert ok is True
    assert err is not None and err.startswith("partial:")
    assert "TaiwanStockPER" in err


def test_total_failure_is_failed():
    ok, err = classify_run_outcome([
        _outcome("failed", 0, error="quota"),
        _outcome("failed", 0, error="quota"),
    ])
    assert ok is False
    assert err is not None and not err.startswith("partial:")
