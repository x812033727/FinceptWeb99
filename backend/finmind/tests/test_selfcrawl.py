"""Tests for `finmind.ingest.selfcrawl` — Phase B source registry.

Verifies:
  - default sources resolve to a stub that raises NotImplementedError
  - 'finmind' resolves to the real FinmindClient
  - register_connector lets a hand-rolled stub override the default
  - resolve_client raises KeyError on unknown source
  - runner picks the source from dataset_sources.active_source
"""
from __future__ import annotations

from datetime import date

import pytest

from finmind.ingest.runner import FinmindClient, ingest_chunk
from finmind.ingest.selfcrawl import (
    _REGISTRY,
    register_connector,
    resolve_client,
)
from finmind.scripts.init_db import seed_dataset_sources


def test_resolve_client_finmind_returns_real_client():
    client = resolve_client("finmind")
    assert isinstance(client, FinmindClient)


def test_resolve_client_default_sources_are_stubbed():
    """Every Phase B source has a stub registered. The stubs must be
    callable but their `fetch` raises with a clear NotImplementedError
    so a premature flip is loud, not silent."""
    for source in ("twse", "tpex", "taifex", "mops", "tdcc"):
        client = resolve_client(source)
        assert client is not None
        assert hasattr(client, "fetch")


@pytest.mark.asyncio
async def test_stub_client_fetch_raises():
    client = resolve_client("twse")
    with pytest.raises(NotImplementedError) as exc:
        await client.fetch("TaiwanStockPrice", "2330", date(2024, 1, 1), date(2024, 1, 31))
    assert "twse" in str(exc.value)


def test_resolve_client_unknown_source_raises():
    with pytest.raises(KeyError) as exc:
        resolve_client("definitely_not_a_real_source")
    assert "no SourceClient registered" in str(exc.value)


@pytest.mark.asyncio
async def test_register_connector_overrides_default():
    """Real Phase B implementations register themselves at import time
    via `register_connector` — verify an override actually replaces
    the stub."""

    class FakeTwseClient:
        async def fetch(self, *args, **kwargs):
            return [{"hello": "from twse stub"}]

    original = _REGISTRY.get("twse")
    try:
        register_connector("twse", lambda: FakeTwseClient())
        client = resolve_client("twse")
        rows = await client.fetch("X", None, date(2024, 1, 1), date(2024, 1, 1))
        assert rows == [{"hello": "from twse stub"}]
    finally:
        if original is not None:
            register_connector("twse", original)


@pytest.mark.asyncio
async def test_runner_routes_to_stub_after_active_source_flip(
    finmind_session,
):
    """Phase A → B headline: flipping `active_source` on a dataset_sources
    row makes ingest_chunk go through the self-crawl stub instead of
    FinMind. Verifies the integration is wired correctly without
    needing real network."""
    await seed_dataset_sources()

    from finmind.models.dataset_source import DatasetSource

    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.active_source = "twse"  # Flip to Phase B
    await finmind_session.commit()

    # No client= arg → runner picks via resolve_client → twse stub.
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )

    assert result.status == "failed"
    assert "not wired yet" in result.error
    assert "twse" in result.error
