"""Auto-runner for enabled FinMind datasets.

Closes the gap between the manual `scripts/backfill.py` (operator-
invoked, one-shot) and "customers expect daily fresh data". The
runner picks up every enabled dataset whose `last_ingest_at` is
stale relative to its `ingest_freq` and calls `ingest_chunk` for
each.

Designed to be invoked from a 15-min cron / k8s CronJob / APScheduler
hook — NOT a long-running daemon. One-shot per invocation keeps the
blast radius small and lets the host's process supervisor handle
restarts. Intentionally no APScheduler dependency in this package.

Two layers, separately testable:

  - `dispatcher.py`  pure logic — given (dataset_sources rows, now)
                     return which are due. No DB writes, no network.
  - `runner.py`      orchestration — call dispatcher, walk results,
                     invoke ingest_chunk for each. DB-aware but
                     network-mockable via the runner's `client` arg.
"""
