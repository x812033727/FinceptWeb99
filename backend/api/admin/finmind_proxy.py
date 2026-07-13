"""Main-app proxy to the FinMind clone subsystem.

Crosses the architectural boundary documented in CLAUDE.md (the
FinMind clone is normally consumed via HTTP, not in-process imports)
ONLY for the AdminPage. Rationale:

  - AdminPage already has main-app JWT admin auth — wiring a separate
    `X-Finmind-Admin-Key` flow into the React app for one card would
    be nonsensical UX.
  - Operator workflow (toggle enabled, watch usage) is purely local
    — there's no scaling reason to round-trip through HTTP.

Customer-facing traffic still goes through `/api/finmind/...` with
its own auth + quota; this proxy is a separate concern.

Endpoints (all gated by main-app admin role):
  GET   /api/admin/finmind/datasets
  PATCH /api/admin/finmind/datasets/{dataset_code}
  GET   /api/admin/finmind/usage?days=N
  GET   /api/admin/finmind/status

The PATCH body schema mirrors `finmind.api.schemas.DatasetSourceUpdate`
so the frontend doesn't have to know it's a proxy.

Implementation note (R9-B2/G8): the endpoints are split by domain into
the `api.admin.finmind` package (datasets/ingest, config+status, plans,
keys) — each sub-module hangs its routes off the shared `router` from
`api.admin.finmind._shared`. This module re-exports that same `router`
object (with the identical route set) so `api.admin.router` and any
other importer keep working unchanged.
"""
from __future__ import annotations

# Import the shared router and every sub-module so their `@router.*`
# decorators run and register the routes onto the single shared router.
from api.admin.finmind import (  # noqa: F401  (imported for route registration)
    config_status,
    datasets,
    keys,
    plans,
)
from api.admin.finmind._shared import router

__all__ = ["router"]
