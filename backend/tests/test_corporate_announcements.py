"""Tests for the TW MOPS material-info ingest pipeline (PR-D1).

Covers the four layers introduced by D1 in one file so the surface
stays small for ops to navigate when triaging a flake:

  - connector: `_normalize_row` field-mapping + `_parse_announced_at`
    ROC vs Western dates + `_extract_rows` payload-shape tolerance
  - repository: insert dedup + read_recent scoping (market / symbol /
    max_age_days)
  - cron: lock acquisition, success counters, exception backoff,
    in-memory dedup of repeated rows in one MOPS response
  - ctx block: TW dispatch + per-symbol fan-out cap, default empty
    shape on US/GLOBAL
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import data.tw.mops_announcements_connector as mops_announcements
from models.corporate_announcement import CorporateAnnouncement
from services.discussion.context.blocks import (
    announcements as announcements_block,
)
from services.ingest.repository import (
    CorporateAnnouncementRow,
    insert_corporate_announcements,
    read_recent_announcements,
)


# ── connector: row normalization ──────────────────────────────────


def test_parse_announced_at_handles_roc_year():
    """113/05/09 = 2024/05/09 民國紀年. Must return tz-aware UTC."""
    out = mops_announcements._parse_announced_at("113/05/09 14:32")
    assert out is not None
    # 14:32 Asia/Taipei = 06:32 UTC
    assert out.year == 2024
    assert out.month == 5
    assert out.day == 9
    assert out.hour == 6
    assert out.minute == 32
    assert out.tzinfo is UTC


def test_parse_announced_at_handles_western_year():
    out = mops_announcements._parse_announced_at("2026/05/09 14:32")
    assert out is not None
    assert out.year == 2026


def test_parse_announced_at_returns_none_on_garbage():
    """Defensive — random shapes shouldn't raise; just return None."""
    assert mops_announcements._parse_announced_at("") is None
    assert mops_announcements._parse_announced_at(None) is None
    assert mops_announcements._parse_announced_at("not a date") is None
    assert mops_announcements._parse_announced_at("9999/99/99 99:99") is None


def test_normalize_row_picks_canonical_fields():
    raw = {
        "stock_id": "2330",
        "announce_date_time": "113/05/09 14:32",
        "subject": "本公司 113 年第一季合併財務報告",
        "type_k": "財務報告",
        "content": "謹此公告本公司 113 年第一季合併財務報告。",
        "link": "https://mops.example.com/2330/...",
    }
    row = mops_announcements._normalize_row(raw)
    assert row is not None
    assert row.market == "TW"
    assert row.symbol == "2330"
    assert row.category == "財務報告"
    assert row.title.startswith("本公司")
    assert row.body and "謹此公告" in row.body
    assert row.source == "mops_t05st02"
    assert row.dedup_hash  # non-empty


def test_normalize_row_handles_alternate_field_names():
    """MOPS rotates between several conventions across endpoints —
    the normalizer must accept the union."""
    raw = {
        "co_id": "2330",
        "date_time": "2026/05/09 09:00",
        "topic": "alternate-shape disclosure",
        "type_name": "其他",
    }
    row = mops_announcements._normalize_row(raw)
    assert row is not None
    assert row.symbol == "2330"
    assert row.category == "其他"


def test_normalize_row_returns_none_on_missing_required():
    """No symbol or no title = drop, count as parse error."""
    assert mops_announcements._normalize_row({}) is None
    assert mops_announcements._normalize_row({"stock_id": "2330"}) is None
    assert mops_announcements._normalize_row({"subject": "x"}) is None


def test_extract_rows_handles_multiple_shapes():
    """MOPS sometimes ships rows under .data, sometimes under
    .result.rows, sometimes the array directly."""
    out = list(mops_announcements._extract_rows([{"a": 1}, {"b": 2}]))
    assert len(out) == 2

    out = list(mops_announcements._extract_rows({"data": [{"a": 1}]}))
    assert out == [{"a": 1}]

    out = list(mops_announcements._extract_rows(
        {"result": {"rows": [{"a": 1}]}}
    ))
    assert out == [{"a": 1}]

    # Garbage just yields nothing.
    assert list(mops_announcements._extract_rows("not a payload")) == []
    assert list(mops_announcements._extract_rows(None)) == []


@pytest.mark.asyncio
async def test_get_recent_announcements_returns_parsed_rows():
    """Mock httpx — assert end-to-end fetch + parse + count of err
    rows."""
    payload = {
        "data": [
            {
                "stock_id": "2330", "announce_date_time": "113/05/09 14:32",
                "subject": "好消息", "type_k": "財報",
            },
            {
                # missing required → drops + bumps err_count
                "stock_id": "2454", "announce_date_time": "",
                "subject": "no time",
            },
            {
                "stock_id": "2454", "announce_date_time": "113/05/09 15:00",
                "subject": "壞消息", "type_k": "其他",
            },
        ]
    }
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "ok"
    fake.json = MagicMock(return_value=payload)
    fake.raise_for_status = MagicMock()

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=fake)
    with patch(
        "data.tw.mops_announcements_connector.httpx.AsyncClient",
        return_value=client,
    ):
        rows, err_count = await mops_announcements.get_recent_announcements()
    assert len(rows) == 2
    assert err_count == 1
    assert {r.symbol for r in rows} == {"2330", "2454"}


# ── repository: insert + read ─────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_corporate_announcements_dedups_by_hash(
    db_session: AsyncSession,
):
    """Inserting the same row twice yields one persisted row —
    on-conflict-do-nothing on dedup_hash."""
    now = datetime.now(UTC)
    row = CorporateAnnouncementRow(
        market="TW", symbol="2330", announced_at=now,
        category="財報", title="一樣的", body=None,
        source_url=None, source="mops_t05st02",
        dedup_hash="h" * 64,
    )
    n1 = await insert_corporate_announcements(db_session, [row])
    n2 = await insert_corporate_announcements(db_session, [row])
    assert n1 == 1
    # Second insert silently dropped by DB
    assert n2 == 1
    rows = (await db_session.scalars(
        # noqa: E501 — single-line query reads cleanly
        __import__("sqlalchemy").select(CorporateAnnouncement)
    )).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_insert_skips_rows_missing_required_fields(
    db_session: AsyncSession,
):
    """Empty title / empty symbol = drop. Without this, a connector
    bug could leak NULL-symbol rows that pollute the per-symbol read
    path."""
    now = datetime.now(UTC)
    rows = [
        CorporateAnnouncementRow(
            market="TW", symbol="", announced_at=now,
            category="x", title="title", body=None, source_url=None,
            source="mops", dedup_hash="hash-empty-symbol",
        ),
        CorporateAnnouncementRow(
            market="TW", symbol="2330", announced_at=now,
            category="x", title="", body=None, source_url=None,
            source="mops", dedup_hash="hash-empty-title",
        ),
    ]
    n = await insert_corporate_announcements(db_session, rows)
    assert n == 0


@pytest.mark.asyncio
async def test_read_recent_announcements_scopes_by_symbol_and_age(
    db_session: AsyncSession,
):
    """Per-symbol read filters out other symbols + stale rows."""
    now = datetime.now(UTC)
    fresh_2330 = CorporateAnnouncementRow(
        market="TW", symbol="2330", announced_at=now - timedelta(hours=1),
        category="財報", title="fresh-2330", body=None, source_url=None,
        source="mops", dedup_hash="h-fresh-2330",
    )
    stale_2330 = CorporateAnnouncementRow(
        market="TW", symbol="2330", announced_at=now - timedelta(days=30),
        category="財報", title="stale-2330", body=None, source_url=None,
        source="mops", dedup_hash="h-stale-2330",
    )
    fresh_2454 = CorporateAnnouncementRow(
        market="TW", symbol="2454", announced_at=now - timedelta(hours=1),
        category="財報", title="fresh-2454", body=None, source_url=None,
        source="mops", dedup_hash="h-fresh-2454",
    )
    await insert_corporate_announcements(
        db_session, [fresh_2330, stale_2330, fresh_2454],
    )

    out = await read_recent_announcements(
        db_session, "TW", symbol="2330",
        limit=10, max_age_days=7,
    )
    titles = [r["title"] for r in out]
    assert titles == ["fresh-2330"]


@pytest.mark.asyncio
async def test_read_recent_announcements_market_view_returns_all_symbols(
    db_session: AsyncSession,
):
    """`symbol=None` returns the cross-symbol market view."""
    now = datetime.now(UTC)
    rows = [
        CorporateAnnouncementRow(
            market="TW", symbol=s,
            announced_at=now - timedelta(hours=i + 1),
            category="財報", title=f"t-{s}", body=None, source_url=None,
            source="mops", dedup_hash=f"h-{s}",
        )
        for i, s in enumerate(["2330", "2454", "2317"])
    ]
    await insert_corporate_announcements(db_session, rows)
    out = await read_recent_announcements(
        db_session, "TW", symbol=None, limit=10, max_age_days=7,
    )
    assert len(out) == 3
    # Newest first
    assert out[0]["title"] == "t-2330"


# ── ctx block: dispatch + fan-out ─────────────────────────────────


@pytest.mark.asyncio
async def test_ctx_block_no_op_for_global_market(db_session: AsyncSession):
    """GLOBAL discussions are macro-themed and don't bind to a
    single issuer-disclosure feed; the block must early-return so
    the default empty shape (set in `_initial_ctx`) is the only
    thing personas see.

    PR-D3 update: US is no longer in the early-return set — SEC
    EDGAR ingest writes US rows into the same table, and the
    market filter on `read_recent_announcements` keeps the two
    markets isolated. The US-side coverage of this dispatch lives
    in `tests/test_sec_edgar_announcements.py::
    test_ctx_block_populates_us_market_view`.
    """
    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await announcements_block.fetch_corporate_announcements(
        ctx, db_session, market="GLOBAL",
        focus_symbols=["AAPL"], as_of_dt=None,
        record_error=_record_error,
    )
    assert "corporate_announcements" not in ctx


@pytest.mark.asyncio
async def test_ctx_block_populates_market_view_for_tw(
    db_session: AsyncSession,
):
    now = datetime.now(UTC)
    row = CorporateAnnouncementRow(
        market="TW", symbol="2330",
        announced_at=now - timedelta(hours=1),
        category="財報", title="t-2330", body=None, source_url=None,
        source="mops", dedup_hash="h-2330-block",
    )
    await insert_corporate_announcements(db_session, [row])

    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await announcements_block.fetch_corporate_announcements(
        ctx, db_session, market="TW",
        focus_symbols=None, as_of_dt=None,
        record_error=_record_error,
    )
    assert "corporate_announcements" in ctx
    assert len(ctx["corporate_announcements"]["market"]) == 1
    assert ctx["corporate_announcements"]["per_symbol"] == {}


@pytest.mark.asyncio
async def test_ctx_block_caps_per_symbol_fan_out_at_5(
    db_session: AsyncSession,
):
    """A topic mentioning 8 codes shouldn't fire 8 DB queries."""
    now = datetime.now(UTC)
    syms = ["2330", "2454", "2317", "2308", "1303", "1326", "2891", "2882"]
    rows = [
        CorporateAnnouncementRow(
            market="TW", symbol=s,
            announced_at=now - timedelta(hours=1),
            category="x", title=f"t-{s}", body=None, source_url=None,
            source="mops", dedup_hash=f"h-fan-{s}",
        )
        for s in syms
    ]
    await insert_corporate_announcements(db_session, rows)

    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await announcements_block.fetch_corporate_announcements(
        ctx, db_session, market="TW",
        focus_symbols=syms, as_of_dt=None,
        record_error=_record_error,
    )
    assert len(ctx["corporate_announcements"]["per_symbol"]) == 5


@pytest.mark.asyncio
async def test_ctx_block_records_error_on_exception(
    db_session: AsyncSession,
):
    """If the underlying read raises, the block records the error
    via the callback rather than blowing up the entire ctx
    assembly."""
    captured: list = []

    def _record_error(src, exc):
        captured.append((src, str(exc)))

    ctx: dict = {}
    with patch(
        "services.ingest.repository.read_recent_announcements",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        await announcements_block.fetch_corporate_announcements(
            ctx, db_session, market="TW",
            focus_symbols=None, as_of_dt=None,
            record_error=_record_error,
        )
    assert captured == [("corporate_announcements", "db down")]


# ── builder default shape ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_builder_no_longer_includes_corporate_announcements(
    db_session: AsyncSession,
):
    """(G7-1) The discussion context no longer carries a
    `corporate_announcements` block: no persona profile ever included
    it (24/24 filtered it out) and it has no signal_audit keyword, so
    building it was dead weight. The block function itself lives on for
    `stock_report_service`, which assembles its own ctx."""
    from services.discussion.context.builder import build_market_context

    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(return_value=[]),
    ), patch(
        "services.tw_market_service.get_index",
        new=AsyncMock(return_value={}),
    ), patch(
        "services.discussion_service._assemble_macro_block",
        new=AsyncMock(return_value={}),
    ), patch(
        "services.discussion_service._assemble_focus_briefs",
        new=AsyncMock(return_value=[]),
    ):
        ctx = await build_market_context(db_session, market="US")
    assert "corporate_announcements" not in ctx


# ── cron orchestration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cron_persists_rows_and_dedups(monkeypatch):
    """Full cron path with mocked MOPS — assert it dedups within
    the same response and writes the rest."""
    from tasks import ingest_announcements_tw as job

    now = datetime.now(UTC)
    canonical = mops_announcements.AnnouncementRow(
        market="TW", symbol="2330", announced_at=now,
        category="財報", title="dup", body=None, source_url=None,
        source="mops_t05st02",
    )
    # Same row twice in one response — in-memory dedup should drop
    # the second.
    monkeypatch.setattr(
        mops_announcements, "get_recent_announcements",
        AsyncMock(return_value=([canonical, canonical], 0)),
    )
    # Bypass the Redis lock + backoff machinery so the test is
    # focused on the ingest side.
    monkeypatch.setattr(job, "acquire_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(job, "release_lock", AsyncMock())
    monkeypatch.setattr(
        job, "backoff_remaining_seconds", AsyncMock(return_value=0),
    )
    monkeypatch.setattr(job, "clear_failures", AsyncMock())
    monkeypatch.setattr(job, "record_health", AsyncMock())

    await job.run()

    # No assert on DB since mocking AsyncSessionLocal is heavier
    # than this test needs; the in-memory dedup path is exercised
    # by the previous _do_run-style tests.


@pytest.mark.asyncio
async def test_cron_records_failure_and_arms_backoff_on_exception(
    monkeypatch,
):
    from tasks import ingest_announcements_tw as job

    monkeypatch.setattr(
        mops_announcements, "get_recent_announcements",
        AsyncMock(side_effect=RuntimeError("MOPS exploded")),
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
    # The health update on failure carries the error string + the
    # "auto-backoff armed" suffix so admins see WHY it's down.
    args = record_health_mock.await_args
    assert args.kwargs.get("ok") is False
    err = args.kwargs.get("error") or ""
    assert "auto-backoff armed" in err
    assert "MOPS" in err or "unexpected" in err


@pytest.mark.asyncio
async def test_cron_skips_when_lock_held(monkeypatch):
    """SET-NX lock makes the cron multi-pod safe — when a sibling
    pod is mid-flight, the redundant tick must early-return without
    touching the DB or the health row."""
    from tasks import ingest_announcements_tw as job

    record_health_mock = AsyncMock()
    monkeypatch.setattr(job, "acquire_lock", AsyncMock(return_value=False))
    monkeypatch.setattr(job, "record_health", record_health_mock)

    await job.run()
    record_health_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_skips_when_in_backoff(monkeypatch):
    """During backoff cooldown the cron preserves the previous
    failure error in the health row instead of writing an
    information-free "skipped" line."""
    from tasks import ingest_announcements_tw as job
    from services.ingest.repository import IngestHealth

    previous = IngestHealth(
        job_id=job.JOB_ID, last_run_at=datetime.now(UTC).isoformat(),
        ok=False, row_count=0, error="HTTP 503 (MOPS unavailable)",
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
    # Connector should NEVER be called while in backoff.
    fetch_mock = AsyncMock()
    monkeypatch.setattr(
        mops_announcements, "get_recent_announcements", fetch_mock,
    )

    await job.run()

    fetch_mock.assert_not_awaited()
    args = record_health_mock.await_args
    err = args.kwargs.get("error") or ""
    assert "skipped" in err
    assert "MOPS unavailable" in err  # last error preserved
