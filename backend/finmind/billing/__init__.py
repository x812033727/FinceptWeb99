"""Billing helpers — Phase 4 sketch.

  - `keys.py`     Issue / revoke / verify API keys
  - `quota.py`    Daily quota counter + 429 enforcement
  - `webhooks.py` Stripe / 綠界 webhook receiver (sketch)

Most of the surface here is stubs / TODOs — the schema lives in
`finmind/models/billing.py` and gets created by migration 0011, but
the operational code (Stripe SDK integration, quota enforcement
middleware, webhook routing) is intentionally left for follow-up
sessions. The shape is committed so future work has a target.
"""
