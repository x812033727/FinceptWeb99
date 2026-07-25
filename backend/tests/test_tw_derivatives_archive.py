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
    # ts, total, mean20 — mean20 is a canned input here (the fake
    # session hands back whatever row this test supplies), so this
    # test only proves the Python-side pct math, not that the SQL
    # actually excludes the latest session from its own baseline.
    # That SQL-level property (`d2.ts < latest.ts`) is verified
    # separately against the live archive — see task-1-report.md.
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
