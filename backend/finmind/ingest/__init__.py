"""FinMind ingest layer.

  - `mappings.py`     FinMind row schema → local table column map
  - `progress.py`     `backfill_progress` ledger CRUD helpers
  - `runner.py`       The driver: query upstream, transform, upsert,
                      record progress

The runner is source-agnostic — `dataset_sources.active_source`
selects the connector. Phase A uses the existing
`data.tw.finmind_connector`; Phase B (post-subscription) will plug in
TWSE / TPEX / TAIFEX / MOPS / TDCC connectors implementing the same
interface (see `runner.SourceClient`)."""
