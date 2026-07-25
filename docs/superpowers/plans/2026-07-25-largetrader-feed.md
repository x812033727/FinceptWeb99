# Large-Trader Futures Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** price_signal discussions see TX large-trader positioning (top5/top10 long/short OI + 特定法人) and dealer-volume breadth from the finmind-schema archive — the first finmind-silo feed, establishing the strategy-routing pattern.

**Architecture:** A read-only, schema-qualified raw-SQL reader (`services/tw_derivatives_archive.py`) → a new error-isolated context block in `blocks/derivatives.py` → `build_market_context` gains a `strategy` param threaded from `discussion.auto_run_strategy`, gating the block to price_signal → persona profiles + prompt annotation so the panel cites it instead of hallucinating it.

**Tech Stack:** Python 3.12, SQLAlchemy async (text() SQL against `finmind.*`), host-venv pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-largetrader-feed-design.md`

## Global Constraints

- Tests: `cd <worktree>/backend && /tmp/fincept-test-venv/bin/python -m pytest ...`; ruff `--select E,W,F --ignore E501`; no wall-clock in tests.
- **Backtest invariant**: with `as_of` set, reads clamp `ts <= as_of`; no live fallback exists here at all (archive-only by design — a gap reads None).
- **No behaviour change outside price_signal**: `strategy=None` (manual discussions, stock reports, other strategies) must be byte-identical.
- Verified data facts (2026-07-25): `finmind.tw_futures_oi_largetraders` rows are `(contract='TX', ts, rank∈{'top5','top10'}, long_oi, short_oi, long_oi_special, short_oi_special)`, newest ts **2026-07-09**; `finmind.tw_futures_dealer_volume` has per-contract rows incl. `contract='total'`, newest **2026-07-17**. Both LAG — the block must surface its data date (`as_of_session`) so personas see staleness; a cadence bump for these two datasets is a recorded follow-up, not this plan.
- Merge ≠ deploy (deploy waits for the post-maiden window). Feature branch off `main`; one PR; CI green → self-merge.

---

### Task 1: Archive reader service

**Files:**
- Create: `backend/services/tw_derivatives_archive.py`
- Test: `backend/tests/test_tw_derivatives_archive.py`

**Interfaces:**
- Produces: `async large_trader_positioning(db, *, as_of: date | None) -> dict | None`. Returns None when the archive has nothing. Shape:

```python
{
  "as_of_session": "2026-07-09",          # the data's own date — staleness visible
  "top5":  {"long_oi": 69609, "short_oi": 59449, "net": 10160,
             "special_long": 66306, "special_short": 59449},
  "top10": {"long_oi": ..., "short_oi": ..., "net": ...,
             "special_long": ..., "special_short": ...},
  "net_change_5s": {"top5": +1234, "top10": -567},   # vs 5 sessions earlier; None if absent
  "dealer_volume": {"as_of_session": "2026-07-17", "total": 45231,
                     "vs_20s_mean_pct": +12.3},        # None-able
}
```

- [ ] **Step 1: Write the failing test** — pure-shape tests via a fake session whose `execute` returns canned rows (SimpleNamespace/tuples), fixed dates:

```python
# backend/tests/test_tw_derivatives_archive.py
"""Reader over the finmind schema (first silo feed). Archive-only: a
gap returns None — no live fallback exists by design, and staleness is
carried on as_of_session so personas see the data's true date."""
from datetime import date
from unittest.mock import AsyncMock

import pytest

from services.tw_derivatives_archive import large_trader_positioning


def _row(ts, rank, lo, so, lsp, ssp):
    return (ts, rank, lo, so, lsp, ssp)


class _DB:
    def __init__(self, batches):
        self._batches = list(batches)

    async def execute(self, *a, **k):
        rows = self._batches.pop(0)
        m = AsyncMock()
        m.all = lambda: rows
        return m


@pytest.mark.asyncio
async def test_builds_shape_from_latest_session():
    latest = [
        _row(date(2026, 7, 9), "top5", 69609, 59449, 66306, 59449),
        _row(date(2026, 7, 9), "top10", 79491, 79459, 76189, 79459),
    ]
    prior = [
        _row(date(2026, 7, 2), "top5", 60000, 59000, 0, 0),
        _row(date(2026, 7, 2), "top10", 70000, 71000, 0, 0),
    ]
    dealer = [(date(2026, 7, 17), 45231.0, 40250.0)]   # ts, total, mean20
    out = await large_trader_positioning(_DB([latest, prior, dealer]), as_of=None)
    assert out["as_of_session"] == "2026-07-09"
    assert out["top5"]["net"] == 69609 - 59449
    assert out["net_change_5s"]["top5"] == (69609 - 59449) - (60000 - 59000)
    assert out["dealer_volume"]["vs_20s_mean_pct"] == pytest.approx(12.38, abs=0.1)


@pytest.mark.asyncio
async def test_empty_archive_returns_none():
    assert await large_trader_positioning(_DB([[], [], []]), as_of=None) is None


@pytest.mark.asyncio
async def test_missing_prior_and_dealer_degrade_to_none_fields():
    latest = [_row(date(2026, 7, 9), "top5", 1, 2, 0, 0),
              _row(date(2026, 7, 9), "top10", 3, 4, 0, 0)]
    out = await large_trader_positioning(_DB([latest, [], []]), as_of=None)
    assert out["net_change_5s"]["top5"] is None
    assert out["dealer_volume"] is None
```

- [ ] **Step 2: fail (ModuleNotFoundError).** **Step 3: Implement** — three `text()` queries (schema-qualified `finmind.`):
  1. latest session ≤ cutoff: `SELECT ts, rank, long_oi, short_oi, long_oi_special, short_oi_special FROM finmind.tw_futures_oi_largetraders WHERE contract='TX' AND (:cutoff IS NULL OR ts <= :cutoff) AND ts = (SELECT max(ts) FROM ... same filter)` — implementer may split into max(ts) then fetch, matching the test's two-batch shape;
  2. the session 5 trading rows earlier (5th distinct ts below the latest) — same shape;
  3. dealer breadth: latest `contract='total'` row's volume + 20-session mean (`SELECT ts, volume, (SELECT avg(volume) FROM (... last 20 sessions ...) t) ...`) — adapt SQL to what the columns allow; the test's canned row is (ts, total, mean20).
  Compute nets/deltas in Python; every absent piece degrades to None per the tests. Docstring: archive-only rationale + the staleness-surface contract + why schema-qualified (same-named compressed public tables exist).
- [ ] **Step 4: green. Step 5: ruff + commit** — `feat(derivatives): finmind-schema large-trader archive reader`

---

### Task 2: Context block

**Files:**
- Modify: `backend/services/discussion/context/blocks/derivatives.py`
- Test: `backend/tests/test_discussion_context_blocks.py` (append)

**Interfaces:**
- Produces: `async fetch_large_trader_positioning(ctx, *, as_of, record_error)` writing `ctx["large_trader_positioning"]` (dict | None); exceptions → `record_error("large_trader_positioning", exc)`, siblings unaffected. Uses its own `AsyncSessionLocal` (autosession pattern like the sibling block — read `fetch_taifex_positioning` and mirror its error isolation; note the reader needs a session — open one inside the block, matching how chip blocks do `*_autosession`).

- [ ] **Step 1: failing tests** (mirror the file's block-test style): (a) happy path patches `services.tw_derivatives_archive.large_trader_positioning` (AsyncMock returning a shape dict) → ctx key set, `as_of` kwarg threaded; (b) reader raising → `ctx["errors"]` entry, ctx key None/absent-safe; (c) reader returning None → ctx key None, no error.
- [ ] **Steps 2-5**: fail → implement → block suite green → ruff + commit — `feat(context): large-trader positioning block`

---

### Task 3: Strategy threading + gate

**Files:**
- Modify: `backend/services/discussion/context/builder.py` (signature + the derivatives fetch site ~line 366 area), `backend/services/discussion_service.py` (`gather_market_context` wrapper ~line 683), `backend/services/discussion/round_runner/loop.py` (call site ~line 159)
- Test: `backend/tests/test_discussion_context_blocks.py` (append)

**Interfaces:**
- Produces: `build_market_context(..., strategy: str | None = None)`; wrapper forwards `strategy`; loop passes `strategy=discussion.auto_run_strategy`. The new block runs ONLY when `strategy == "price_signal"` (both live and backtest — replay parity per spec). `_initial_ctx` gains `"large_trader_positioning": None` (the always-present contract from #268's fix round applies here too).

- [ ] **Step 1: failing tests**: (a) builder with `strategy="price_signal"` invokes the block (patch it with a spy) and threads `as_of=info_cutoff`; (b) `strategy=None` and `strategy="chip_quality"` → block NOT invoked, ctx key stays None (present, from `_initial_ctx`); (c) backtest + price_signal → invoked with the backtest cutoff (replay parity).
- [ ] **Steps 2-5**: fail → implement (default None keeps every existing caller byte-identical — `stock_report_service` untouched) → block+builder suites green + `pytest tests/ -q -k "discussion or context"` regression sweep → ruff + commit — `feat(context): strategy-routed block gating; price_signal sees large traders`

---

### Task 4: Persona exposure + annotation

**Files:**
- Modify: `backend/services/discussion/persona_config.py` (add `"large_trader_positioning"` to every profile frozenset that currently contains `"taifex_positioning"` — lines ~235/252/274/288; verify by grep, do not miss the SHORT_TERM derivation), `backend/services/discussion/prompts.py` (`_BLOCK_ANNOTATIONS` entry describing the fields in the panel's schema language: top5/top10 多空 OI、特定法人、5 日淨變化、自營商總量 vs 20 日均、as_of_session 為資料日)
- Test: `backend/tests/test_persona_config.py` or the nearest existing persona-profile test file (find it; assert the new key rides wherever taifex_positioning does, and that `_filter_context_for_persona` passes it through for a macro-profile persona)

- [ ] Standard TDD steps; commit — `feat(personas): large-trader block exposure + prompt annotation`

---

### Task 5: PR + measurement (operator)

- [ ] **Step 1:** Full suites (backend + finmind, two processes) + collect-only + ruff.
- [ ] **Step 2:** PR `feat(context): price_signal sees TX large-trader positioning (map step-2, feed 1)`. Body: the silo context (39 write-only datasets; personas hallucinating this signal class), the strategy-routing pattern established, the staleness caveat (data dates surfaced; cadence bump follow-up recorded), replay-parity note, deploy gating.
- [ ] **Step 3:** CI green → merge. Do NOT deploy.
- [ ] **Step 4 (operator, needs user cost approval ~US$15):** replay A/B on the stratified sessions (with/without the block via a temporary env/flag or two batches around the merge boundary — decide with the controller at run time): compare abstention, derivatives-class hallucination-warning count, D5 excess. Result gates feed 2 (借券→chip_quality).

---

## Self-review notes

- Spec coverage: reader (T1), block (T2), routing structural piece (T3), personas/annotation (T4), measurement-as-operator-step + next-candidate gating (T5). Staleness surfaced (T1 shape + global facts). Cadence bump = recorded follow-up, correctly out of scope.
- Type consistency: reader returns `dict | None` consumed by block; block key matches `_initial_ctx` default and profile string.
- Adaptation points flagged: autosession idiom in the block (T2), profile derivation chains (T4), exact SQL split (T1).
