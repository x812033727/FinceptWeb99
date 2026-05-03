"""Public HTTP surface for the FinMind clone.

  - `router.py` — `/api/finmind/data/{code}` (the FinMind-clone API
                  endpoint customers consume) and `/api/finmind/admin/...`
                  (operator-facing CRUD on `dataset_sources`).
  - `schemas.py` — Pydantic response models.
  - `auth.py`   — API-key check (stubbed in Phase 3, real keys land
                  with Phase 4 billing).

Designed to be mounted by the main FinceptWeb app *or* run as a
standalone microservice — `router.router` doesn't depend on anything
in `backend/api/*`.
"""
