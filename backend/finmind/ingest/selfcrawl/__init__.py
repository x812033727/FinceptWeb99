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

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

log = logging.getLogger("finmind.selfcrawl")


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


def _taifex_client_factory() -> Any:
    """Late-import factory for the TAIFEX daily futures/options connector."""
    from finmind.ingest.selfcrawl.taifex import TaifexClient

    return TaifexClient()


def _tdcc_client_factory() -> Any:
    """Late-import factory for the TDCC 股權分散 connector."""
    from finmind.ingest.selfcrawl.tdcc import TdccClient

    return TdccClient()


def _fred_client_factory() -> Any:
    """Late-import factory for the FRED macro (bond yield / crude oil)
    connector."""
    from finmind.ingest.selfcrawl.fred import FredClient

    return FredClient()


def _binance_client_factory() -> Any:
    """Late-import factory for the crypto OHLCV connector — keeps the
    selfcrawl package import-light when crypto deps aren't needed."""
    from finmind.ingest.selfcrawl.binance import BinanceClient

    return BinanceClient()


def _coingecko_client_factory() -> Any:
    """Late-import factory for the CoinGecko market-cap-info connector."""
    from finmind.ingest.selfcrawl.coingecko import CoingeckoClient

    return CoingeckoClient()


_REGISTRY: dict[str, ConnectorFactory] = {
    "twse":      _twse_client_factory,
    "tpex":      lambda: _NotWiredYetClient("tpex"),
    "taifex":    _taifex_client_factory,
    "mops":      _mops_client_factory,
    "tdcc":      _tdcc_client_factory,
    "fred":      _fred_client_factory,
    "binance":   _binance_client_factory,
    "coingecko": _coingecko_client_factory,
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


def known_sources() -> set[str]:
    """Every source string the runner can route — `finmind` plus every
    registered self-crawl connector (real or stub).

    The AdminPage flip endpoints use this as the outer allowlist so the
    valid-source set can't silently drift from the registry the way a
    hand-maintained literal did (it was missing fred/binance/coingecko,
    blocking macro + crypto cutover). Membership here only means "known
    routing target"; `is_source_implemented`/`covers_dataset` still gate
    whether the flip is actually safe for a given dataset."""
    return {"finmind", *_REGISTRY}


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


# ── Per-dataset coverage map ────────────────────────────────────
#
# `is_source_implemented(source)` answers "does THIS source have a real
# connector wired up at all?" — coarse-grained, returns True as soon
# as one dataset is handled. `supported_datasets(source)` answers
# "exactly which dataset_codes does this source's connector know how
# to fetch?" — finer-grained, used by the AdminPage flip gatekeeper to
# reject e.g. flipping `TaiwanFuturesDaily` to source='twse' (TWSE has
# a working client but no futures handler).
#
# Each real source's module exposes its own `supported_datasets()`
# function returning a sorted list of handled dataset_codes; the
# registry below late-imports them so this package stays import-light.
SupportedDatasetsProvider = Callable[[], set[str]]


def _twse_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.twse import supported_datasets as _s

    return set(_s())


def _mops_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.mops import supported_datasets as _s

    return set(_s())


def _taifex_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.taifex import supported_datasets as _s

    return set(_s())


def _tdcc_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.tdcc import supported_datasets as _s

    return set(_s())


def _fred_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.fred import supported_datasets as _s

    return set(_s())


def _binance_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.binance import supported_datasets as _s

    return set(_s())


def _coingecko_supported_datasets() -> set[str]:
    from finmind.ingest.selfcrawl.coingecko import supported_datasets as _s

    return set(_s())


def _finmind_supported_datasets() -> set[str]:
    """FinMind covers every dataset in the catalog — the whole point of
    Phase A is the paid sub serves all 80. Load lazily because catalog
    import pulls the upstream-truth registry."""
    from finmind.dataset_catalog import all_entries

    return {entry.dataset_code for _, entry in all_entries()}


_SUPPORTED_DATASETS_PROVIDERS: dict[str, SupportedDatasetsProvider] = {
    "finmind": _finmind_supported_datasets,
    "twse":      _twse_supported_datasets,
    "taifex":    _taifex_supported_datasets,
    "tdcc":      _tdcc_supported_datasets,
    "fred":      _fred_supported_datasets,
    "mops":      _mops_supported_datasets,
    "binance":   _binance_supported_datasets,
    "coingecko": _coingecko_supported_datasets,
    # tpex currently routes to `_NotWiredYetClient`, so its supported set
    # is empty by definition. A real connector registers here via
    # `register_supported_datasets()` below at import time alongside
    # `register_connector()`.
}


def register_supported_datasets(
    source: str, provider: SupportedDatasetsProvider
) -> None:
    """Companion to `register_connector` — when a Phase B module wires
    its real client, it should also tell the registry which dataset
    codes it covers so the AdminPage gatekeeper can pre-emptively
    reject flips to unsupported (source, dataset) combinations."""
    _SUPPORTED_DATASETS_PROVIDERS[source] = provider


def supported_datasets(source: str) -> set[str]:
    """Return the set of dataset_codes the given source can fetch.

    Returns an empty set for sources that are stubbed
    (`_NotWiredYetClient`) or unknown — the AdminPage uses
    `dataset in supported_datasets(target_source)` to decide whether
    the flip is safe. An empty result is loud-by-design: stub
    sources can never accept flips."""
    provider = _SUPPORTED_DATASETS_PROVIDERS.get(source)
    if provider is None:
        return set()
    try:
        return provider()
    except Exception:
        # A misconfigured provider shouldn't break the AdminPage. Log
        # loudly so the operator sees it and treat as unsupported —
        # the flip will be rejected, the alternative (silent True)
        # would let the operator break ingest by flipping to a source
        # whose `supported_datasets()` import is broken.
        log.exception("supported_datasets(%r) provider raised", source)
        return set()


def covers_dataset(source: str, dataset_code: str) -> bool:
    """Convenience predicate — `True` when `source` can serve
    `dataset_code` end-to-end. Equivalent to
    `dataset_code in supported_datasets(source)` but reads better at
    the call site (e.g. AdminPage gatekeeper)."""
    return dataset_code in supported_datasets(source)
