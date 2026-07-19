"""Regression tests for secret-safe FinMind telemetry boundaries."""
from __future__ import annotations

from finmind.api.schemas import DatasetSourceItem
from finmind.redaction import REDACTED, redact_exception, redact_secret_text


def test_redact_secret_text_removes_query_token_and_preserves_context():
    message = (
        "request failed for https://example.test/data?dataset=Prices&"
        "token=test-query-secret&start_date=2026-01-01"
    )

    result = redact_secret_text(message)

    assert result is not None
    assert "test-query-secret" not in result
    assert f"token={REDACTED}" in result
    assert "dataset=Prices" in result
    assert "start_date=2026-01-01" in result


def test_redact_secret_text_handles_common_credential_shapes():
    message = (
        "Authorization: Bearer test-bearer-secret; "
        "postgresql://user:test-db-secret@db.example/app; "
        "JWT eyJheader.payload.signature; "
        "payload={'api_key': 'test-json-secret'}"
    )

    result = redact_secret_text(message)

    assert result is not None
    for secret in (
        "test-bearer-secret",
        "test-db-secret",
        "eyJheader.payload.signature",
        "test-json-secret",
    ):
        assert secret not in result
    assert result.count(REDACTED) == 4


def test_redact_secret_text_passes_plain_text_and_none():
    assert redact_secret_text("connection reset") == "connection reset"
    assert redact_secret_text(None) is None


def test_redact_exception_uses_safe_repr():
    exc = RuntimeError(
        "https://example.test/data?token=test-exception-secret&dataset=X"
    )

    result = redact_exception(exc)

    assert result.startswith("RuntimeError(")
    assert "test-exception-secret" not in result
    assert f"token={REDACTED}" in result


def test_dataset_source_schema_redacts_legacy_last_error():
    item = DatasetSourceItem(
        dataset_code="Example",
        category="technical",
        description_zh="example",
        local_table="example_table",
        per_symbol=False,
        primary_source="finmind",
        fallback_source=None,
        active_source="finmind",
        enabled=True,
        sponsor_tier=False,
        ingest_freq="daily",
        last_ingest_at=None,
        last_ingest_rows=0,
        last_error=(
            "https://example.test/data?token=test-legacy-secret&dataset=X"
        ),
    )

    assert item.last_error is not None
    assert "test-legacy-secret" not in item.last_error
    assert f"token={REDACTED}" in item.last_error
