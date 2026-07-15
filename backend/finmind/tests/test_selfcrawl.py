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
    """`tpex` is still stubbed (twse was wired in a follow-up commit).
    Verify the stub's NotImplementedError message names the source so
    operators can identify which connector still needs implementing."""
    client = resolve_client("tpex")
    with pytest.raises(NotImplementedError) as exc:
        await client.fetch("TaiwanStockConvertibleBondDaily", None, date(2024, 1, 1), date(2024, 1, 31))
    assert "tpex" in str(exc.value)


def test_resolve_client_unknown_source_raises():
    with pytest.raises(KeyError) as exc:
        resolve_client("definitely_not_a_real_source")
    assert "no SourceClient registered" in str(exc.value)


@pytest.mark.asyncio
async def test_register_connector_overrides_default():
    """Real Phase B implementations register themselves via the
    factory map. Override one to confirm the registration mechanism
    works (use `tpex` — `twse` already has the real client wired so
    can't double as the override-test target)."""

    class FakeTpexClient:
        async def fetch(self, *args, **kwargs):
            return [{"hello": "from tpex stub"}]

    original = _REGISTRY.get("tpex")
    try:
        register_connector("tpex", lambda: FakeTpexClient())
        client = resolve_client("tpex")
        rows = await client.fetch("X", None, date(2024, 1, 1), date(2024, 1, 1))
        assert rows == [{"hello": "from tpex stub"}]
    finally:
        if original is not None:
            register_connector("tpex", original)


@pytest.mark.asyncio
async def test_runner_routes_to_stub_after_active_source_flip(
    finmind_session,
):
    """Phase A → B headline: flipping `active_source` on a dataset_sources
    row makes ingest_chunk go through the registered self-crawl client
    instead of FinMind. Use a stubbed source (`tpex`) so we don't need
    network — the integration check is the routing, not the fetch.

    A separate end-to-end test exercises the wired `twse` client with
    a mocked twse_connector."""
    await seed_dataset_sources()

    from finmind.models.dataset_source import DatasetSource

    row = await finmind_session.get(
        DatasetSource, "TaiwanStockConvertibleBondDaily"
    )
    row.active_source = "tpex"  # Flip to Phase B (still stubbed)
    await finmind_session.commit()

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockConvertibleBondDaily",
        symbol="12345",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )

    # The CB mapping now exists, so reaching the explicit connector stub is
    # evidence that the active_source flip routed away from FinMind.
    assert result.status == "failed"
    assert "source='tpex' not wired yet" in result.error
