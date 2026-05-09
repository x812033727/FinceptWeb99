"""FinMind clone API router — aggregator.

Three sibling modules under `routes/` each export their own
`APIRouter`; this file mounts them onto a single top-level router so
external imports (`from finmind.api.router import router`) keep
working unchanged. Public surface from `routes.data`, operator-only
endpoints from `routes.admin`, and the Stripe webhook from
`routes.billing`.

  GET    /api/finmind/datasets
  GET    /api/finmind/catalog
  GET    /api/finmind/data/{dataset_code}
  GET    /api/finmind/data/{dataset_code}/export
  GET    /api/finmind/admin/datasets
  PATCH  /api/finmind/admin/datasets/{dataset_code}
  GET    /api/finmind/admin/usage
  POST   /api/finmind/webhooks/stripe
"""
from __future__ import annotations

from fastapi import APIRouter

from finmind.api.routes.admin import router as admin_router
from finmind.api.routes.billing import router as billing_router
from finmind.api.routes.data import router as data_router

router = APIRouter()
router.include_router(data_router)
router.include_router(admin_router)
router.include_router(billing_router)
