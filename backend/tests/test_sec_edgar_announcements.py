"""Tests for the US SEC EDGAR 8-K ingest pipeline (PR-D3).

Covers the four layers introduced by D3:

  - Connector: ATOM parsing (`_normalize_entry`), CIK extraction
    (`_extract_cik`), padding handling, ticker-map miss path
    (foreign filer / SPV silently dropped).
  - Cron: lock acquisition, success counters, exception backoff,
    in-memory dedup of repeated entries.
  - Ctx block: US-market dispatch reads the same
    `corporate_announcements` table as TW; per-symbol fan-out
    works on US tickers without per-market branching.
  - Schema reuse: US rows persist via the same
    `insert_corporate_announcements` path as TW; the read tier's
    market filter keeps the two markets isolated.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import data.us.sec_edgar_connector as sec_edgar
from services.discussion.context.blocks import (
    announcements as announcements_block,
)
from services.ingest.repository import (
    CorporateAnnouncementRow,
    insert_corporate_announcements,
    read_recent_announcements,
)


# ── helpers ──────────────────────────────────────────────────────


def _atom_feed(entries: list[dict]) -> bytes:
    """Build a minimal ATOM feed payload for the connector to
    parse. Each entry dict supplies title / updated / link / summary;
    missing keys are simply omitted from the XML so we can exercise
    the parser's defensive paths."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
    ]
    for e in entries:
        lines.append("<entry>")
        if "title" in e:
            lines.append(f"<title>{e['title']}</title>")
        if "updated" in e:
            lines.append(f"<updated>{e['updated']}</updated>")
        if "link" in e:
            lines.append(f'<link href="{e["link"]}" />')
        if "summary" in e:
            lines.append(f"<summary>{e['summary']}</summary>")
        lines.append("</entry>")
    lines.append("</feed>")
    return "\n".join(lines).encode("utf-8")


def _patch_httpx(payload: bytes, *, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = payload
    resp.raise_for_status = MagicMock()
    if status >= 400:
        import httpx
        err = httpx.HTTPStatusError("boom", request=MagicMock(), response=resp)
        resp.raise_for_status = MagicMock(side_effect=err)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return patch(
        "data.us.sec_edgar_connector.httpx.AsyncClient",
        return_value=client,
    )


@pytest.fixture(autouse=True)
def _reset_ticker_map_cache():
    """The connector keeps a process-level CIK→ticker map cache so
    repeated cron ticks don't re-fetch the 13K-row JSON. Tests need
    to reset it between cases so a monkeypatched
    `get_cik_ticker_map` is observable."""
    sec_edgar._reset_caches_for_tests()
    yield
    sec_edgar._reset_caches_for_tests()


# ── parsing ──────────────────────────────────────────────────────


def test_extract_cik_from_canonical_title():
    out = sec_edgar._extract_cik(
        "8-K - Apple Inc. (0000320193) (Filer)",
    )
    assert out == "0000320193"


def test_extract_cik_returns_none_on_no_match():
    assert sec_edgar._extract_cik("") is None
    assert sec_edgar._extract_cik("malformed title without parens") is None
    # Non-CIK number in parens (e.g. amount, date) — we only match
    # exactly 10 digits, so a 4-digit year would NOT be picked up.
    assert sec_edgar._extract_cik("title with (2024)") is None


def test_strip_padding_normalizes_to_unpadded_int_form():
    """SEC's company_tickers.json keys are unpadded ints; EDGAR's
    feed uses zero-padded 10-digit. Both normalize to the same
    lookup key."""
    assert sec_edgar._strip_padding("0000320193") == "320193"
    assert sec_edgar._strip_padding("320193") == "320193"
    # Defensive: all-zero CIK shouldn't strip down to empty string
    # (which would .get() into a different bucket).
    assert sec_edgar._strip_padding("0000000000") == "0"


def test_parse_announced_at_iso_z_suffix():
    out = sec_edgar._parse_announced_at("2026-05-09T13:30:00Z")
    assert out == datetime(2026, 5, 9, 13, 30, tzinfo=UTC)


def test_parse_announced_at_iso_explicit_offset():
    out = sec_edgar._parse_announced_at("2026-05-09T13:30:00+00:00")
    assert out == datetime(2026, 5, 9, 13, 30, tzinfo=UTC)


def test_parse_announced_at_returns_none_on_garbage():
    assert sec_edgar._parse_announced_at(None) is None
    assert sec_edgar._parse_announced_at("") is None
    assert sec_edgar._parse_announced_at("not a date") is None


def test_normalize_entry_picks_canonical_fields():
    entry_xml = (
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        '<title>8-K - Apple Inc. (0000320193) (Filer)</title>'
        '<updated>2026-05-09T13:30:00Z</updated>'
        '<link href="https://www.sec.gov/cgi-bin/browse-edgar?...&amp;CIK=0000320193" />'
        '<summary>Form 8-K material event</summary>'
        '</entry>'
    )
    entry = ET.fromstring(entry_xml)
    row = sec_edgar._normalize_entry(
        entry, cik_to_ticker={"320193": "AAPL"},
    )
    assert row is not None
    assert row.market == "US"
    assert row.symbol == "AAPL"
    assert row.category == "8-K"
    assert "Apple" in row.title
    assert row.body == "Form 8-K material event"
    assert row.source == "sec_edgar_8k"
    assert row.source_url and "0000320193" in row.source_url


def test_normalize_entry_drops_when_ticker_unmappable():
    """Foreign filer / private SPV — no ticker mapping. Drop
    silently rather than persist a CIK as the symbol (which would
    make per-symbol read paths useless)."""
    entry_xml = (
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        '<title>8-K - Foreign Filer Co (0009999999) (Filer)</title>'
        '<updated>2026-05-09T13:30:00Z</updated>'
        '</entry>'
    )
    entry = ET.fromstring(entry_xml)
    row = sec_edgar._normalize_entry(
        entry, cik_to_ticker={"320193": "AAPL"},  # filer's CIK absent
    )
    assert row is None


def test_normalize_entry_drops_when_required_field_missing():
    # Missing title
    e1 = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        '<updated>2026-05-09T13:30:00Z</updated></entry>'
    )
    assert sec_edgar._normalize_entry(e1, cik_to_ticker={"1": "X"}) is None
    # Missing updated
    e2 = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        '<title>8-K - Co. (0000000001) (Filer)</title></entry>'
    )
    assert sec_edgar._normalize_entry(e2, cik_to_ticker={"1": "X"}) is None
    # Missing CIK in title
    e3 = ET.fromstring(
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        '<title>8-K malformed</title>'
        '<updated>2026-05-09T13:30:00Z</updated></entry>'
    )
    assert sec_edgar._normalize_entry(e3, cik_to_ticker={"1": "X"}) is None


# ── connector end-to-end ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_8k_filings_returns_parsed_rows():
    payload = _atom_feed([
        {
            "title": "8-K - Apple Inc. (0000320193) (Filer)",
            "updated": "2026-05-09T13:30:00Z",
            "link": "https://www.sec.gov/aapl/index.html",
            "summary": "Material event",
        },
        {
            # Drops on missing CIK in title.
            "title": "8-K malformed entry",
            "updated": "2026-05-09T14:00:00Z",
        },
        {
            "title": "8-K - Microsoft Corp. (0000789019) (Filer)",
            "updated": "2026-05-09T14:30:00Z",
            "link": "https://www.sec.gov/msft/index.html",
        },
    ])
    with patch.object(
        sec_edgar, "get_cik_ticker_map",
        return_value={"320193": "AAPL", "789019": "MSFT"},
    ):
        with _patch_httpx(payload):
            rows, err_count = await sec_edgar.get_recent_8k_filings()
    assert len(rows) == 2
    assert err_count == 1
    assert {r.symbol for r in rows} == {"AAPL", "MSFT"}


@pytest.mark.asyncio
async def test_get_recent_8k_filings_handles_empty_feed_gracefully():
    payload = _atom_feed([])
    with patch.object(
        sec_edgar, "get_cik_ticker_map", return_value={"1": "X"},
    ):
        with _patch_httpx(payload):
            rows, err_count = await sec_edgar.get_recent_8k_filings()
    assert rows == []
    assert err_count == 0


@pytest.mark.asyncio
async def test_get_recent_8k_filings_swallows_atom_parse_failure():
    """Connector must not crash on malformed XML — yield no entries
    so the cron writes ok=true/0 instead of a hard failure that
    arms backoff."""
    with patch.object(
        sec_edgar, "get_cik_ticker_map", return_value={},
    ):
        with _patch_httpx(b"<not-xml>}{[", status=200):
            rows, err_count = await sec_edgar.get_recent_8k_filings()
    assert rows == []
    assert err_count == 0


# ── ticker map cache ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticker_map_uses_in_process_cache_after_first_fetch():
    """The 13K-row CIK→ticker map should fetch ONCE per process,
    not per cron tick."""
    fetched = {"320193": "AAPL"}
    fetch_mock = AsyncMock(return_value=fetched)
    # Make Redis cache miss + force the SEC fetch path.
    with patch(
        "data.us.sec_edgar_connector.cache_get",
        new=AsyncMock(return_value=None),
    ), patch(
        "data.us.sec_edgar_connector.cache_set",
        new=AsyncMock(),
    ), patch.object(
        sec_edgar, "_fetch_ticker_map_from_sec", new=fetch_mock,
    ):
        m1 = await sec_edgar.get_cik_ticker_map()
        m2 = await sec_edgar.get_cik_ticker_map()
    assert m1 == fetched
    assert m2 == fetched
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ticker_map_returns_empty_on_redis_miss_and_sec_failure():
    """Cold-start with both Redis miss + SEC down: return empty
    map. The cron then drops every row of that tick (incrementing
    err_count) but doesn't crash."""
    with patch(
        "data.us.sec_edgar_connector.cache_get",
        new=AsyncMock(return_value=None),
    ), patch.object(
        sec_edgar, "_fetch_ticker_map_from_sec",
        new=AsyncMock(return_value=None),
    ):
        out = await sec_edgar.get_cik_ticker_map()
    assert out == {}


@pytest.mark.asyncio
async def test_ticker_map_loads_from_redis_when_present():
    import json
    cached_payload = json.dumps({"320193": "AAPL"})
    fetch_mock = AsyncMock()  # must NOT be called
    with patch(
        "data.us.sec_edgar_connector.cache_get",
        new=AsyncMock(return_value=cached_payload),
    ), patch.object(
        sec_edgar, "_fetch_ticker_map_from_sec", new=fetch_mock,
    ):
        out = await sec_edgar.get_cik_ticker_map()
    assert out == {"320193": "AAPL"}
    fetch_mock.assert_not_called()


# ── ctx block: US dispatch ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ctx_block_populates_us_market_view(db_session: AsyncSession):
    """Symmetry with TW: same table, same read tier, same default
    shape — only the per-row `market` field differs."""
    now = datetime.now(UTC)
    row = CorporateAnnouncementRow(
        market="US", symbol="AAPL",
        announced_at=now - timedelta(hours=1),
        category="8-K", title="Apple 8-K material event",
        body=None, source_url=None,
        source="sec_edgar_8k", dedup_hash="h-aapl-test",
    )
    await insert_corporate_announcements(db_session, [row])

    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await announcements_block.fetch_corporate_announcements(
        ctx, db_session, market="US",
        focus_symbols=None, as_of_dt=None,
        record_error=_record_error,
    )
    assert "corporate_announcements" in ctx
    assert len(ctx["corporate_announcements"]["market"]) == 1
    assert ctx["corporate_announcements"]["market"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_ctx_block_per_symbol_fan_out_works_on_us_tickers(
    db_session: AsyncSession,
):
    now = datetime.now(UTC)
    rows = [
        CorporateAnnouncementRow(
            market="US", symbol=sym,
            announced_at=now - timedelta(hours=1),
            category="8-K", title=f"{sym} disclosure",
            body=None, source_url=None,
            source="sec_edgar_8k", dedup_hash=f"h-us-{sym}",
        )
        for sym in ["AAPL", "MSFT", "GOOGL"]
    ]
    await insert_corporate_announcements(db_session, rows)

    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await announcements_block.fetch_corporate_announcements(
        ctx, db_session, market="US",
        focus_symbols=["AAPL", "MSFT"], as_of_dt=None,
        record_error=_record_error,
    )
    per = ctx["corporate_announcements"]["per_symbol"]
    assert set(per.keys()) == {"AAPL", "MSFT"}
    assert per["AAPL"][0]["title"] == "AAPL disclosure"


@pytest.mark.asyncio
async def test_ctx_block_no_op_for_global_market(db_session: AsyncSession):
    """GLOBAL discussions are macro-themed; no per-issuer feed binds.
    The block must early-return so the default empty shape from
    `_initial_ctx` is what personas see."""
    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await announcements_block.fetch_corporate_announcements(
        ctx, db_session, market="GLOBAL",
        focus_symbols=None, as_of_dt=None,
        record_error=_record_error,
    )
    assert "corporate_announcements" not in ctx


@pytest.mark.asyncio
async def test_ctx_block_keeps_markets_isolated(db_session: AsyncSession):
    """Insert one TW + one US row; the TW discussion must NOT see
    the US row even though they share a table."""
    now = datetime.now(UTC)
    await insert_corporate_announcements(db_session, [
        CorporateAnnouncementRow(
            market="TW", symbol="2330",
            announced_at=now - timedelta(hours=1),
            category="財報", title="台積電財報",
            body=None, source_url=None,
            source="mops_t05st02", dedup_hash="h-iso-tw",
        ),
        CorporateAnnouncementRow(
            market="US", symbol="AAPL",
            announced_at=now - timedelta(hours=1),
            category="8-K", title="Apple 8-K",
            body=None, source_url=None,
            source="sec_edgar_8k", dedup_hash="h-iso-us",
        ),
    ])

    tw_rows = await read_recent_announcements(db_session, "TW")
    us_rows = await read_recent_announcements(db_session, "US")

    assert {r["symbol"] for r in tw_rows} == {"2330"}
    assert {r["symbol"] for r in us_rows} == {"AAPL"}


# ── cron orchestration ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_persists_rows_and_dedups(monkeypatch):
    """Same in-memory dedup test as the TW cron — one MOPS / SEC
    response sometimes repeats a row when the issuer files an
    amendment; the in-memory check keeps the per-call payload
    clean before the DB-level on-conflict fires."""
    from tasks import ingest_announcements_us as job

    now = datetime.now(UTC)
    canonical = sec_edgar.FilingRow(
        market="US", symbol="AAPL", announced_at=now,
        category="8-K", title="dup", body=None, source_url=None,
        source="sec_edgar_8k",
    )
    monkeypatch.setattr(
        sec_edgar, "get_recent_8k_filings",
        AsyncMock(return_value=([canonical, canonical], 0)),
    )
    monkeypatch.setattr(job, "acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(job, "release_lock", AsyncMock())
    monkeypatch.setattr(
        job, "backoff_remaining_seconds", AsyncMock(return_value=0),
    )
    monkeypatch.setattr(job, "clear_failures", AsyncMock())
    monkeypatch.setattr(job, "record_health", AsyncMock())

    await job.run()


@pytest.mark.asyncio
async def test_cron_records_failure_and_arms_backoff_on_exception(
    monkeypatch,
):
    from tasks import ingest_announcements_us as job

    monkeypatch.setattr(
        sec_edgar, "get_recent_8k_filings",
        AsyncMock(side_effect=RuntimeError("SEC exploded")),
    )
    monkeypatch.setattr(job, "acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(job, "release_lock", AsyncMock())
    monkeypatch.setattr(
        job, "backoff_remaining_seconds", AsyncMock(return_value=0),
    )
    record_failure_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(job, "record_failure", record_failure_mock)
    record_health_mock = AsyncMock()
    monkeypatch.setattr(job, "record_health", record_health_mock)

    await job.run()
    record_failure_mock.assert_awaited_once()
    args = record_health_mock.await_args
    assert args.kwargs.get("ok") is False
    err = args.kwargs.get("error") or ""
    assert "auto-backoff armed" in err
    assert "SEC" in err or "unexpected" in err


@pytest.mark.asyncio
async def test_cron_skips_when_lock_held(monkeypatch):
    from tasks import ingest_announcements_us as job

    record_health_mock = AsyncMock()
    monkeypatch.setattr(job, "acquire_lock", AsyncMock(return_value=False))
    monkeypatch.setattr(job, "record_health", record_health_mock)

    await job.run()
    record_health_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_preserves_last_error_in_backoff(monkeypatch):
    from tasks import ingest_announcements_us as job
    from services.ingest.repository import IngestHealth

    previous = IngestHealth(
        job_id=job.JOB_ID, last_run_at=datetime.now(UTC).isoformat(),
        ok=False, row_count=0,
        error="HTTP 429 (SEC rate-limit; back off)",
    )
    monkeypatch.setattr(job, "acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(job, "release_lock", AsyncMock())
    monkeypatch.setattr(
        job, "backoff_remaining_seconds", AsyncMock(return_value=120),
    )
    monkeypatch.setattr(job, "get_failure_count", AsyncMock(return_value=2))
    monkeypatch.setattr(job, "get_health", AsyncMock(return_value=previous))
    record_health_mock = AsyncMock()
    monkeypatch.setattr(job, "record_health", record_health_mock)
    fetch_mock = AsyncMock()
    monkeypatch.setattr(sec_edgar, "get_recent_8k_filings", fetch_mock)

    await job.run()
    fetch_mock.assert_not_awaited()
    args = record_health_mock.await_args
    err = args.kwargs.get("error") or ""
    assert "skipped" in err
    assert "rate-limit" in err  # last error preserved
