"""Dataset catalog + ingest endpoints for the AdminPage FinMind proxy.

Covers listing/updating dataset sources and the manual ingest triggers
(single-dataset run, run-all-due, and the curated quick-start enable set).
"""
from __future__ import annotations

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from finmind.models.dataset_source import DatasetSource

from ._shared import AdminUser, FmDb, _ensure_finmind_db_reachable, log, router


class FinmindDatasetItem(BaseModel):
    """One row in `GET /finmind/datasets`. Schema duplicated from
    `finmind.api.schemas.DatasetSourceItem` (rather than re-exported)
    so the AdminPage frontend has a stable contract that's independent
    of the FinMind subsystem's internal Pydantic versioning."""

    dataset_code: str
    category: str
    description_zh: str
    local_table: str
    per_symbol: bool
    primary_source: str
    fallback_source: str | None
    active_source: str
    enabled: bool
    sponsor_tier: bool
    ingest_freq: str
    last_ingest_at: str | None
    last_ingest_rows: int | None
    last_error: str | None


class FinmindDatasetUpdate(BaseModel):
    enabled: bool | None = None
    active_source: str | None = None


@router.get(
    "/datasets",
    response_model=list[FinmindDatasetItem],
    summary="AdminPage: list every FinMind dataset",
)
async def list_finmind_datasets(_: AdminUser, db: FmDb) -> list[FinmindDatasetItem]:
    await _ensure_finmind_db_reachable(db)
    rows = (
        await db.execute(
            select(DatasetSource).order_by(DatasetSource.dataset_code)
        )
    ).scalars().all()
    return [
        FinmindDatasetItem(
            dataset_code=r.dataset_code,
            category=r.category,
            description_zh=r.description_zh,
            local_table=r.local_table,
            per_symbol=r.per_symbol,
            primary_source=r.primary_source,
            fallback_source=r.fallback_source,
            active_source=r.active_source,
            enabled=r.enabled,
            sponsor_tier=r.sponsor_tier,
            ingest_freq=r.ingest_freq,
            last_ingest_at=(
                r.last_ingest_at.isoformat() if r.last_ingest_at else None
            ),
            last_ingest_rows=r.last_ingest_rows,
            last_error=r.last_error,
        )
        for r in rows
    ]


@router.patch(
    "/datasets/{dataset_code}",
    response_model=FinmindDatasetItem,
    summary="AdminPage: toggle enabled / flip active_source per dataset",
)
async def update_finmind_dataset(
    dataset_code: str,
    body: FinmindDatasetUpdate,
    _: AdminUser,
    db: FmDb,
) -> FinmindDatasetItem:
    row = await db.get(DatasetSource, dataset_code)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.active_source is not None:
        # Two-layer gatekeeper (outer allowlist derived from the
        # connector registry via known_sources() so it can't drift):
        #   1. is_source_implemented() — does the source even have a
        #      real connector? Catches flips to fully-stubbed
        #      sources (tpex currently).
        #   2. covers_dataset() — does THAT connector know how to
        #      fetch THIS dataset? Catches partial coverage, e.g.
        #      flipping `TaiwanFuturesDaily` to source='twse' (TWSE
        #      client exists but doesn't handle futures).
        # Both raise loud 400s with the exact remediation hint so the
        # operator can grep the codebase and find the missing piece.
        from finmind.ingest.selfcrawl import (
            covers_dataset,
            is_source_implemented,
            known_sources,
        )

        valid_sources = known_sources()
        if body.active_source not in valid_sources:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source must be one of "
                    f"{sorted(valid_sources)}; got '{body.active_source}'"
                ),
            )
        if not is_source_implemented(body.active_source):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source='{body.active_source}' has no "
                    f"connector wired up yet — flipping to it would "
                    f"break the next ingest cycle. Implement the "
                    f"connector in finmind/ingest/selfcrawl/"
                    f"{body.active_source}.py + register_connector() "
                    f"before flipping."
                ),
            )
        if not covers_dataset(body.active_source, dataset_code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source='{body.active_source}' has a "
                    f"connector but no handler for dataset "
                    f"'{dataset_code}'. Add a `_fetch_*` entry to "
                    f"finmind/ingest/selfcrawl/{body.active_source}"
                    f".py:_DISPATCH before flipping, OR keep the "
                    f"current active_source for this dataset."
                ),
            )
        row.active_source = body.active_source
    await db.commit()
    await db.refresh(row)
    return FinmindDatasetItem(
        dataset_code=row.dataset_code,
        category=row.category,
        description_zh=row.description_zh,
        local_table=row.local_table,
        per_symbol=row.per_symbol,
        primary_source=row.primary_source,
        fallback_source=row.fallback_source,
        active_source=row.active_source,
        enabled=row.enabled,
        sponsor_tier=row.sponsor_tier,
        ingest_freq=row.ingest_freq,
        last_ingest_at=(
            row.last_ingest_at.isoformat() if row.last_ingest_at else None
        ),
        last_ingest_rows=row.last_ingest_rows,
        last_error=row.last_error,
    )


class RunDatasetRequest(BaseModel):
    """Optional body for `POST /datasets/{code}/run`. All fields
    optional — defaults match the cron's behavior for that dataset."""

    symbol: str | None = None
    start_date: str | None = None  # YYYY-MM-DD
    end_date: str | None = None


class RunDatasetResult(BaseModel):
    """One ChunkResult flattened for the frontend."""

    dataset_code: str
    symbol: str | None
    range_start: str
    range_end: str
    status: str           # 'done' | 'failed' | 'skipped'
    rows_written: int
    error: str | None


@router.post(
    "/datasets/{dataset_code}/run",
    response_model=RunDatasetResult,
    summary="AdminPage: trigger one ingest_chunk synchronously",
)
async def run_dataset(
    dataset_code: str,
    body: RunDatasetRequest,
    _: AdminUser,
    db: FmDb,
) -> RunDatasetResult:
    """Manual one-shot ingest for a single dataset. Synchronous —
    waits for the ingest to complete and returns the outcome so the
    AdminPage can show success/failure inline.

    For per-symbol datasets, supplying `symbol` runs that one symbol;
    omitting it falls through to symbol=None (most upstream sources
    fail without a symbol — runner records 'failed' in
    backfill_progress with a clear error).

    `start_date` / `end_date` default to the last 7 days (matches the
    cron's default backfill window).
    """
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    from finmind.ingest.runner import ingest_chunk
    from finmind.models.dataset_source import DatasetSource as DS

    ds = await db.get(DS, dataset_code)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if not ds.local_table:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{dataset_code}: destination table not yet built — "
                "Phase 1 hasn't migrated this dataset"
            ),
        )

    end = (
        _dt.strptime(body.end_date, "%Y-%m-%d").date()
        if body.end_date
        else _dt.now(tz=_tz.utc).date()
    )
    start = (
        _dt.strptime(body.start_date, "%Y-%m-%d").date()
        if body.start_date
        else end - _td(days=7)
    )

    # Wrap in try/except so a runtime error inside ingest_chunk OR
    # any of its transitive dependencies (FinMind connector, db
    # transaction, mapping resolution) surfaces as a 500 with a
    # human-readable detail field instead of FastAPI's default
    # generic 500-page. Frontend reads `error.response.data.detail`
    # to render the actual cause.
    try:
        result = await ingest_chunk(
            db,
            dataset_code=dataset_code,
            symbol=body.symbol,
            range_start=start,
            range_end=end,
        )
    except Exception as exc:
        log.exception("run_dataset crashed: %s", dataset_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"ingest_chunk raised: {exc.__class__.__name__}: {exc!s}",
        ) from exc

    return RunDatasetResult(
        dataset_code=dataset_code,
        symbol=body.symbol,
        range_start=start.isoformat(),
        range_end=end.isoformat(),
        status=result.status,
        rows_written=result.rows_written,
        error=result.error,
    )


class RunDueResponse(BaseModel):
    """`POST /run-due` summary — frontend renders the totals + a
    per-chunk breakdown."""

    total: int
    done: int
    failed: int
    skipped: int
    rows_written: int
    outcomes: list[RunDatasetResult]


@router.post(
    "/run-due",
    response_model=RunDueResponse,
    summary=(
        "AdminPage: trigger run_due_now (every enabled dataset whose "
        "last_ingest_at is stale, fanned across tw_stock_info universe)"
    ),
)
async def run_due(_: AdminUser, db: FmDb) -> RunDueResponse:
    """Manual "refresh now" button — same logic as the cron auto-runner.

    Walks every enabled dataset whose last_ingest_at is stale relative
    to its ingest_freq, and for per-symbol datasets fans across the
    universe currently in `tw_stock_info`. Synchronous — the operator
    waits for the full sweep to complete (typically a few seconds
    when most datasets are fresh).

    NOT designed for the initial-deploy "ingest every dataset for
    every symbol for 10 years of history" workflow — that's the
    backfill CLI's job. This is the daily refresh button."""
    from finmind.scheduler.runner import (
        get_universe_from_tw_stock_info,
        run_due_now,
    )

    # Same defensive try/except pattern as run_dataset above —
    # surface the real cause (e.g. "OperationalError: relation
    # 'tw_stock_info' does not exist" → catalog seed didn't land,
    # or "ConnectionRefusedError" → finmind_clone DB not reachable)
    # to the frontend instead of a generic 500.
    try:
        universe = await get_universe_from_tw_stock_info(db)
        outcomes = await run_due_now(db, symbols=universe)
    except Exception as exc:
        log.exception("run_due crashed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"run_due_now raised: {exc.__class__.__name__}: "
                f"{exc!s}. Most likely cause: finmind_clone DB "
                f"unreachable at FINMIND_DATABASE_URL — start the "
                f"`postgres_finmind` container and restart the backend "
                f"so the lifespan auto-init can migrate + seed."
            ),
        ) from exc

    items = [
        RunDatasetResult(
            dataset_code=o.chunk.dataset_code,
            symbol=o.chunk.symbol,
            range_start=o.chunk.range_start.isoformat(),
            range_end=o.chunk.range_end.isoformat(),
            status=o.result.status,
            rows_written=o.result.rows_written,
            error=o.result.error,
        )
        for o in outcomes
    ]
    return RunDueResponse(
        total=len(items),
        done=sum(1 for i in items if i.status == "done"),
        failed=sum(1 for i in items if i.status == "failed"),
        skipped=sum(1 for i in items if i.status == "skipped"),
        rows_written=sum(i.rows_written for i in items),
        outcomes=items,
    )


# ── Quick Start (enable curated default set) ────────────────────


# Curated default set surfaced to first-run operators. Picked for:
#   - covers the most-asked questions in TW equities (price, chip flow,
#     valuation, fundamentals, news)
#   - all have verified ingest mappings (item 4 / item 5 work landed)
#   - mix of per-symbol + market-wide so the operator sees both
#     ingest patterns work
#
# Order matters: TaiwanStockInfo first so the symbol universe gets
# populated before per-symbol datasets fan out.
_QUICK_START_DATASETS: tuple[str, ...] = (
    # Master data first — every per-symbol dataset's cron loop fans
    # across this universe.
    "TaiwanStockInfo",
    # Headline price + chip flow.
    "TaiwanStockPrice",
    "TaiwanStockInstitutionalInvestorsBuySell",
    "TaiwanStockMarginPurchaseShortSale",
    # Fundamentals (monthly + quarterly).
    "TaiwanStockMonthRevenue",
    # Valuation + market cap.
    "TaiwanStockPER",
    "TaiwanStockMarketValue",
    "TaiwanStockShareholding",
    # Corporate actions.
    "TaiwanStockDividend",
    # Market-wide (no per-symbol fan-out, runs daily without universe).
    "TaiwanStockTotalInstitutionalInvestors",
    "TaiwanStockTotalMarginPurchaseShortSale",
)


class QuickStartResponse(BaseModel):
    enabled_count: int
    skipped: list[str]   # codes that couldn't be enabled (no local_table etc.)
    enabled: list[str]   # codes successfully flipped enabled=true
    note: str            # human-readable next-step prompt


@router.post(
    "/quick-start",
    response_model=QuickStartResponse,
    summary=(
        "AdminPage: bulk-enable a curated set of headline datasets"
    ),
)
async def quick_start(_: AdminUser, db: FmDb) -> QuickStartResponse:
    """Enables the curated default set in one click. Each dataset is
    only flipped when:
      - it exists in dataset_sources (catalog seeded)
      - it has a non-empty local_table (Phase 1 migration applied)
      - it isn't already enabled (idempotent)

    Does NOT trigger an ingest run — operator clicks "Run all due now"
    afterward. Reason: ingest can take seconds-to-minutes (especially
    if TaiwanStockInfo's universe is big) and we want the quick-start
    response to come back instantly so the UI feels responsive. The
    explicit two-click flow also gives the operator a chance to review
    what got enabled before triggering work.
    """
    enabled_codes: list[str] = []
    skipped_codes: list[str] = []

    for code in _QUICK_START_DATASETS:
        row = await db.get(DatasetSource, code)
        if row is None:
            skipped_codes.append(f"{code} (not in catalog)")
            continue
        if not row.local_table:
            skipped_codes.append(f"{code} (no local_table — Phase 1 incomplete)")
            continue
        if row.enabled:
            # Already enabled — count as enabled but not as "newly
            # enabled" so the response is honest about what changed.
            enabled_codes.append(code)
            continue
        row.enabled = True
        enabled_codes.append(code)

    await db.commit()

    note = (
        f"Enabled {len(enabled_codes)} datasets. Click 'Run all due "
        f"now' below to trigger the first ingest. TaiwanStockInfo "
        f"will populate the symbol universe so per-symbol datasets "
        f"can fan out on the next run."
        if enabled_codes else
        "Nothing to enable — either every recommended dataset was "
        "already on, or Phase 1 migrations haven't built their "
        "destination tables yet."
    )

    return QuickStartResponse(
        enabled_count=len(enabled_codes),
        enabled=enabled_codes,
        skipped=skipped_codes,
        note=note,
    )
