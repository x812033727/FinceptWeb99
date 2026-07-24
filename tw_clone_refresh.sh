#!/bin/bash
# TW FinMind-clone daily refresh (Phase A) for finceptweb99.
#
# MARKET-WIDE ONLY (--skip-per-symbol): ~35 datasets, ~1 request each, so a
# full run costs well under 100 FinMind calls. Deliberately does NOT fan
# TaiwanStockPriceAdj across the ~2050-symbol equity universe: the account's
# effective quota is ~550 req/hr and is SHARED with production's sponsor-tier
# crons (07:00-11:30 UTC), so a daily 2050-request PriceAdj fan-out would
# starve them. PriceAdj changes only on corporate actions — refresh it on a
# separate weekly/off-peak drip or on demand, not here.
#
# run_due is idempotent (UPSERT) and only touches datasets that are "due".
# Schedule this well clear of the production window — 16:00 UTC (00:00 Taipei).
set -euo pipefail
cd /opt/finceptweb99
DC=/usr/local/bin/docker-compose
$DC exec -T backend python -m finmind.scripts.run_due --skip-per-symbol
