"""Discussion market-context builder.

Public entry point: `build_market_context(db, *, market, as_of, ...)`.

Internal layout:
  - `builder.py` — orchestrator: builds the default ctx, fans out
    HTTP-bound blocks via `asyncio.gather`, runs DB-bound blocks
    serially on the shared session.
  - `blocks/http.py`  — screener, index, macro, focus_briefs (HTTP)
  - `blocks/chip.py`  — TW chip metrics + revenue + buybacks (DB)
  - `blocks/risk.py`  — TW risk warnings (DB)
  - `blocks/news.py`  — market + international + per-symbol news (DB)
  - `blocks/owner.py` — user_context + prior_discussions (DB)

Each block is one async function with the shape
`fetch_X(ctx, *, ..., record_error) -> None`. Blocks read from `ctx`
for shared state (rare) and write their results into `ctx[<key>]`.
Failures are isolated per block via `record_error` so one connector
outage doesn't blank the whole context.
"""
from .builder import build_market_context

__all__ = ["build_market_context"]
