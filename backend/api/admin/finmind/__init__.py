"""Domain-split sub-modules for the AdminPage FinMind proxy.

Endpoints are defined on the shared `router` in `_shared.py` and grouped
here by domain (datasets/ingest, config+status, plans, keys). The public
entry point remains `api.admin.finmind_proxy` which re-exports that same
`router` object after importing every sub-module to register its routes.
"""
