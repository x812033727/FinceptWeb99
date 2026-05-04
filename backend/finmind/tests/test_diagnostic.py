"""Tests for `finmind.scripts.diagnostic` — the support-bundle export.

The headline invariant: secrets never appear in the output.
Verifying this is a meaningful safety check — the diagnostic output
typically gets pasted into chat / email / GitHub issues.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from finmind.models.backfill_progress import BackfillProgress
from finmind.models.billing import ApiUsageEvent
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.diagnostic import (
    _redact_url_password,
    collect_diagnostic,
    collect_environment,
)
from finmind.scripts.init_db import seed_dataset_sources


# ── Pure URL redaction ──────────────────────────────────────────


def test_redact_url_password_replaces_password():
    url = "postgresql+asyncpg://user:supersecret@host:5432/db"
    out = _redact_url_password(url)
    assert "supersecret" not in out
    assert "<redacted>" in out
    # Scheme + user + host + db preserved (operator can still see
    # where the connector points).
    assert "user" in out
    assert "host:5432/db" in out


def test_redact_url_password_handles_no_password_url():
    """SQLite URL has no password segment — should pass through
    unchanged."""
    url = "sqlite+aiosqlite:///:memory:"
    assert _redact_url_password(url) == url


def test_redact_url_password_handles_empty():
    assert _redact_url_password("") == ""


# ── Environment capture ─────────────────────────────────────────


def test_collect_environment_redacts_secret_vars(monkeypatch):
    monkeypatch.setenv("FINMIND_TOKEN", "hunter2-real-token")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_supersecret")
    monkeypatch.setenv(
        "FINMIND_DATABASE_URL",
        "postgresql+asyncpg://finmind:dbpass@db:5432/clone",
    )

    env = collect_environment()
    assert env["FINMIND_TOKEN"] == "<redacted>"
    assert env["STRIPE_WEBHOOK_SECRET"] == "<redacted>"
    # Password redacted but host/db kept for diagnosis.
    assert "dbpass" not in env["FINMIND_DATABASE_URL"]
    assert "<redacted>" in env["FINMIND_DATABASE_URL"]
    assert "db:5432/clone" in env["FINMIND_DATABASE_URL"]


def test_collect_environment_distinguishes_empty_from_set(monkeypatch):
    """Operator needs to know "is FINMIND_TOKEN even set?". The
    redacted value should differ between empty and present so the
    diagnosis is meaningful."""
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    env = collect_environment()
    assert env["FINMIND_TOKEN"] == ""

    monkeypatch.setenv("FINMIND_TOKEN", "any-non-empty-value")
    env = collect_environment()
    assert env["FINMIND_TOKEN"] == "<redacted>"


# ── End-to-end bundle ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_diagnostic_returns_all_sections(finmind_session):
    """Smoke: every section exists in the output. Tolerates per-
    section errors gracefully."""
    diag = await collect_diagnostic()
    assert "generated_at" in diag
    assert "environment" in diag
    assert "status" in diag
    assert "backfill_recent" in diag
    assert "usage_recent" in diag
    assert "dataset_telemetry" in diag


@pytest.mark.asyncio
async def test_collect_diagnostic_skip_usage(finmind_session):
    """--skip-usage replaces the section with a sentinel string —
    useful when the table is huge."""
    diag = await collect_diagnostic(skip_usage=True)
    assert diag["usage_recent"] == "skipped (--skip-usage)"


@pytest.mark.asyncio
async def test_collect_diagnostic_includes_recent_backfill_rows(
    finmind_session,
):
    """Insert 3 progress rows; bundle should include them in
    backfill_recent."""
    finmind_session.add_all([
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2330",
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="done",
            rows_written=22,
            finished_at=datetime.now(tz=timezone.utc),
        ),
        BackfillProgress(
            dataset_code="TaiwanStockPrice",
            symbol="2317",
            source="finmind",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            status="failed",
            error_message="connection reset",
            finished_at=datetime.now(tz=timezone.utc),
        ),
    ])
    await finmind_session.commit()

    diag = await collect_diagnostic()
    rows = diag["backfill_recent"]
    assert isinstance(rows, list)
    assert len(rows) == 2
    by_status = {r["status"] for r in rows}
    assert by_status == {"done", "failed"}
    # Error message included (truncated at 300).
    failed = next(r for r in rows if r["status"] == "failed")
    assert "connection reset" in failed["error_message"]


@pytest.mark.asyncio
async def test_collect_diagnostic_usage_groups_by_status_code(
    finmind_session,
):
    finmind_session.add_all([
        ApiUsageEvent(
            ts=datetime.now(tz=timezone.utc),
            api_key_id=1,
            dataset_code="TaiwanStockPrice",
            endpoint="/data/TaiwanStockPrice",
            row_count=10,
            bytes_out=100,
            latency_ms=5,
            status_code=200,
        ),
        ApiUsageEvent(
            ts=datetime.now(tz=timezone.utc),
            api_key_id=1,
            dataset_code="TaiwanStockPrice",
            endpoint="/data/TaiwanStockPrice",
            row_count=0,
            bytes_out=0,
            latency_ms=2,
            status_code=429,
        ),
    ])
    await finmind_session.commit()

    diag = await collect_diagnostic()
    usage = diag["usage_recent"]
    assert usage["sample_size"] == 2
    assert usage["by_status_code"]["200"] == 1
    assert usage["by_status_code"]["429"] == 1


@pytest.mark.asyncio
async def test_collect_diagnostic_dataset_telemetry_carries_last_error(
    finmind_session,
):
    """dataset_telemetry section should include last_error so a
    silently-broken dataset is visible without trawling the
    backfill_progress table."""
    await seed_dataset_sources()

    row = await finmind_session.get(
        DatasetSource, "TaiwanStockPrice",
    )
    row.last_error = "FinMind 503 retry exhausted"
    row.last_ingest_at = datetime.now(tz=timezone.utc)
    row.last_ingest_rows = 0
    await finmind_session.commit()

    diag = await collect_diagnostic()
    by_code = {
        d["dataset_code"]: d for d in diag["dataset_telemetry"]
    }
    assert "FinMind 503" in by_code["TaiwanStockPrice"]["last_error"]


@pytest.mark.asyncio
async def test_collect_diagnostic_secrets_never_in_serialized_output(
    finmind_session, monkeypatch,
):
    """Headline safety invariant — operator can paste the full JSON
    output anywhere without leaking the FINMIND_TOKEN."""
    import json as _json

    monkeypatch.setenv(
        "FINMIND_TOKEN", "hunter2-this-must-NOT-leak",
    )
    monkeypatch.setenv(
        "FINMIND_DATABASE_URL",
        "postgresql://u:dbpass-must-NOT-leak@h:5432/d",
    )

    diag = await collect_diagnostic()
    serialized = _json.dumps(diag, default=str)
    assert "hunter2-this-must-NOT-leak" not in serialized
    assert "dbpass-must-NOT-leak" not in serialized
    # Sanity: the URL host *is* still in the output.
    assert "h:5432/d" in serialized
