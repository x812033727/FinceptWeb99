"""Phase B self-crawl connectors — replacement source registry.

When the FinMind subscription expires, every dataset's
`active_source` flips from 'finmind' to one of the upstream sources
listed below. The runner picks a connector based on
`dataset_sources.active_source`; this package houses the
implementations.

Plan B mapping (one entry per dataset_code in
`finmind/dataset_catalog.py`):

  finmind                    → (paid; current default)
  twse        TWSE OpenAPI   → STOCK_DAY, STOCK_DAY_AVG_ALL, T86,
                               BFI82U, MI_QFIIS, MI_MARGN, ...
  tpex        TPEX           → 上櫃 quotes, CB info / daily / inst
  taifex      期交所          → futures + options daily / inst /
                               large traders
  mops        公開觀測站       → quarterly statements + monthly revenue
  tdcc        集保結算所        → 股權分散表 (weekly)

Each connector here implements the same `SourceClient` Protocol that
`finmind.ingest.runner` expects, so swapping the source is a one-line
UPDATE on `dataset_sources.active_source` per row — no runner code
change.

Phase 5 in this commit: REGISTRY skeleton + per-source stub clients
that raise `NotImplementedError` with a clear "wire me up before Phase
B cutover" message. Each upstream actually has a working connector
already in `backend/data/tw/{twse,tpex,taifex,mops}_connector.py` —
the work in a follow-up session is wrapping those (already-tested)
HTTP clients in this package's `SourceClient` shape, dataset by
dataset.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any


class _NotWiredYetClient:
    """Default stub — raises with a clear message naming the source so
    the operator can grep for which connector still needs wiring."""

    def __init__(self, source: str) -> None:
        self._source = source

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            f"self-crawl connector for source='{self._source}' "
            f"not wired yet (dataset {dataset_code}). Implement "
            f"finmind/ingest/selfcrawl/{self._source}.py before "
            f"flipping active_source on this dataset."
        )


# ── Connector registry ──────────────────────────────────────────
#
# Map source name → factory. The runner's `FinmindClient` already
# handles 'finmind'; everything else routes here.

ConnectorFactory = Callable[[], Any]


def _twse_client_factory() -> Any:
    """Late-imported factory — keeps the selfcrawl package import-safe
    when `data.tw.twse_connector` isn't installed (e.g. in a stripped-
    down deployment that only runs the FinMind A path)."""
    from finmind.ingest.selfcrawl.twse import TwseClient

    return TwseClient()


def _mops_client_factory() -> Any:
    """Same late-import shape as _twse — see docstring above."""
    from finmind.ingest.selfcrawl.mops import MopsClient

    return MopsClient()


_REGISTRY: dict[str, ConnectorFactory] = {
    "twse":   _twse_client_factory,
    "tpex":   lambda: _NotWiredYetClient("tpex"),
    "taifex": lambda: _NotWiredYetClient("taifex"),
    "mops":   _mops_client_factory,
    "tdcc":   lambda: _NotWiredYetClient("tdcc"),
}


def is_source_implemented(source: str) -> bool:
    """True when `source` has a real connector wired up; False when
    it's a stub or unknown.

    `finmind` is always implemented (FinmindClient lives in
    `finmind.ingest.runner` and is imported lazily). Other sources
    are real iff their factory does NOT return `_NotWiredYetClient`.
    Used by the AdminPage PATCH endpoint to reject `active_source`
    flips to stubbed sources before the cron blows up at runtime
    with `NotImplementedError`."""
    if source == "finmind":
        return True
    factory = _REGISTRY.get(source)
    if factory is None:
        return False
    try:
        instance = factory()
    except Exception:
        return False
    return not isinstance(instance, _NotWiredYetClient)


def register_connector(source: str, factory: ConnectorFactory) -> None:
    """Hot-pluggable registration — the eventual real implementations
    in `selfcrawl/{twse,tpex,...}.py` call this at import time so
    `runner.resolve_client` finds them without an explicit import."""
    _REGISTRY[source] = factory


def resolve_client(source: str) -> Any:
    """Map dataset_sources.active_source → SourceClient instance.

    Raises KeyError when an unknown source string lands in the DB —
    surface the bad config loudly rather than silently falling back."""
    if source == "finmind":
        # Late import to keep this module free of network deps.
        from finmind.ingest.runner import FinmindClient

        return FinmindClient()

    factory = _REGISTRY.get(source)
    if factory is None:
        raise KeyError(
            f"no SourceClient registered for active_source='{source}'. "
            f"Known sources: {['finmind', *sorted(_REGISTRY)]}"
        )
    return factory()
