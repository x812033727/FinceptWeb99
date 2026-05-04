"""FinMind clone API router.

Public endpoints (consumed by paying customers):
  GET  /api/finmind/data/{dataset_code}
       Mirrors FinMind's `api/v4/data` endpoint shape — same query
       params (data_id, start_date, end_date) plus `limit` cap.
  GET  /api/finmind/datasets
       Catalog discovery — list of available datasets, descriptions,
       freshness. No admin key needed; same shape FinMind exposes.

Admin endpoints (operator-only):
  GET    /api/finmind/admin/datasets
  PATCH  /api/finmind/admin/datasets/{dataset_code}
       Toggle enabled / flip active_source for Phase A → B transition.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from finmind.api.auth import AuthContext, require_admin_key, require_api_key
from finmind.api.schemas import (
    DataResponse,
    DataResponseMetadata,
    DatasetSourceItem,
    DatasetSourceUpdate,
    PublicCatalogItem,
)
from finmind.billing.quota import check_quota, record_usage
from finmind.billing.stripe_webhook import (
    SignatureError,
    process_event,
    verify_signature,
)
from finmind.db.session import get_finmind_db
from finmind.models.billing import Plan, Subscription
from finmind.models.dataset_source import DatasetSource

log = logging.getLogger("finmind.api")

router = APIRouter()


# ── Public catalog discovery ─────────────────────────────────────


@router.get(
    "/datasets",
    response_model=list[DatasetSourceItem],
    summary="List every FinMind dataset and its current freshness",
)
async def list_datasets(
    db: AsyncSession = Depends(get_finmind_db),
    _auth: AuthContext = Depends(require_api_key),
) -> list[DatasetSourceItem]:
    """Catalog discovery — no quota cost, no per-row charge.
    Customers shouldn't have to spend their daily allowance just to
    figure out what datasets exist."""
    rows = (
        await db.execute(select(DatasetSource).order_by(DatasetSource.dataset_code))
    ).scalars().all()
    return [DatasetSourceItem.model_validate(r, from_attributes=True) for r in rows]


@router.get(
    "/catalog",
    response_model=list[PublicCatalogItem],
    summary="Public catalog (no API key) — drops operator-only fields",
)
async def public_catalog(
    db: AsyncSession = Depends(get_finmind_db),
) -> list[PublicCatalogItem]:
    """Marketing-facing catalog. Auth-free so prospective customers
    can browse what's on offer before they have a key. Returns a
    slimmed-down shape — operational state (last_error, last_ingest_at,
    active_source) stays in `/datasets` + `/admin/datasets`."""
    rows = (
        await db.execute(select(DatasetSource).order_by(DatasetSource.dataset_code))
    ).scalars().all()
    return [
        PublicCatalogItem(
            dataset_code=r.dataset_code,
            category=r.category,
            description_zh=r.description_zh,
            ingest_freq=r.ingest_freq,
            per_symbol=r.per_symbol,
            sponsor_tier=r.sponsor_tier,
            available=bool(r.enabled and r.local_table),
        )
        for r in rows
    ]


# ── Public data query ────────────────────────────────────────────


# Whitelist of columns that may appear in WHERE clauses. We do NOT
# accept arbitrary user-supplied column names because the local-
# table identifier is interpolated into raw SQL — every operator-
# facing column must be one of these well-known names.
_WHERE_COLUMNS = {"symbol", "ts", "stock_id", "contract", "cb_id"}


def _coerce_date(s: str | None) -> date | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


async def _authorize_dataset_scope(
    db: AsyncSession, auth: AuthContext, dataset_code: str
) -> None:
    """Raise 403 if the caller's key doesn't cover `dataset_code`.

    Two-level check:
      1. `api_keys.scope_datasets` (per-key override) — if set,
         dataset_code must be in the list.
      2. Otherwise fall back to the subscription's plan
         `allowed_datasets` — if set, dataset_code must be in the list.

    Allowlist=NULL means "all datasets allowed" at that level. Operator/
    dev-mode requests (api_key=None) bypass scope entirely — they're
    implicitly trusted."""
    if auth.api_key is None:
        return

    per_key = auth.api_key.scope_datasets
    if per_key:
        if dataset_code not in per_key:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"this key's scope_datasets does not include "
                    f"{dataset_code!r}"
                ),
            )
        return

    sub_id = auth.api_key.subscription_id
    if sub_id is None:
        return
    sub = await db.get(Subscription, sub_id)
    if sub is None:
        return
    plan = await db.get(Plan, sub.plan_code)
    if plan is None or not plan.allowed_datasets:
        return
    if dataset_code not in plan.allowed_datasets:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"plan {plan.code!r} does not include dataset "
                f"{dataset_code!r}"
            ),
        )


@router.get(
    "/data/{dataset_code}",
    response_model=DataResponse,
    summary="Query a FinMind dataset's local mirror by symbol + date range",
)
async def get_data(
    dataset_code: str,
    response: Response,
    data_id: str | None = Query(None, description="Symbol / contract / cb_id."),
    start_date: str | None = Query(None, description="YYYY-MM-DD inclusive."),
    end_date: str | None = Query(None, description="YYYY-MM-DD inclusive."),
    limit: int = Query(10000, le=100000, ge=1),
    db: AsyncSession = Depends(get_finmind_db),
    auth: AuthContext = Depends(require_api_key),
) -> DataResponse:
    started = time.monotonic()

    # Scope gate — 403 if this key isn't allowed to read this dataset.
    # Runs before quota so an out-of-scope request doesn't burn a
    # quota slot.
    await _authorize_dataset_scope(db, auth, dataset_code)

    # Quota gate (real `fck_live_` keys only — env-allowlist + dev
    # mode skip the quota path because operators set their own caps).
    quota = None
    if auth.api_key is not None:
        quota = await check_quota(db, auth.api_key)
        # `Remaining` reports the budget AFTER this call lands —
        # subtract 1 (clamped to 0) so a customer counting down sees
        # the same number on header N as on the next /usage poll.
        post_call_remaining = max(0, quota.calls_remaining - 1)
        response.headers["X-RateLimit-Limit"] = str(quota.daily_call_limit)
        response.headers["X-RateLimit-Remaining"] = str(post_call_remaining)
        response.headers["X-RateLimit-Reset"] = str(int(quota.reset_at.timestamp()))
        if not quota.permitted:
            await _safe_record_usage(
                db,
                api_key_id=auth.api_key.id,
                dataset_code=dataset_code,
                endpoint=f"/data/{dataset_code}",
                row_count=0,
                bytes_out=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                status_code=429,
                error_message=quota.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=quota.reason or "quota exhausted",
                headers={
                    "Retry-After": str(int(
                        (quota.reset_at - datetime.now(quota.reset_at.tzinfo))
                        .total_seconds()
                    )),
                },
            )

    ds = await db.get(DatasetSource, dataset_code)
    if ds is None:
        await _safe_record_usage(
            db,
            api_key_id=auth.api_key.id if auth.api_key else None,
            dataset_code=dataset_code,
            endpoint=f"/data/{dataset_code}",
            row_count=0,
            bytes_out=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            status_code=404,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if not ds.local_table:
        await _safe_record_usage(
            db,
            api_key_id=auth.api_key.id if auth.api_key else None,
            dataset_code=dataset_code,
            endpoint=f"/data/{dataset_code}",
            row_count=0,
            bytes_out=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            status_code=503,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{dataset_code}: destination table not yet built "
                "(Phase 1 in progress for this dataset)"
            ),
        )

    where: list[str] = []
    params: dict[str, Any] = {}

    if data_id:
        # Per-symbol vs per-contract vs per-cb_id — try to match the
        # column shape to the table. PR-of-truth heuristic: prefer
        # `symbol` (per-symbol tables) and fall back to `contract`
        # (futures/options) or `cb_id` (CB tables) by inspection of
        # the table's column list.
        col = await _detect_id_column(db, ds.local_table)
        if col is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{dataset_code} ({ds.local_table}) has no per-row "
                    "identity column — drop `data_id` from your query"
                ),
            )
        where.append(f"{col} = :data_id")
        params["data_id"] = data_id

    sd = _coerce_date(start_date)
    ed = _coerce_date(end_date)
    if sd:
        where.append("ts >= :start_date")
        params["start_date"] = sd
    if ed:
        where.append("ts <= :end_date")
        params["end_date"] = ed

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    # `limit` is bound — Pydantic Query already constrained it to a
    # bounded int. Local table name is sourced from `dataset_sources`,
    # not user input, so direct interpolation is safe.
    sql = (
        f"SELECT * FROM {ds.local_table}{where_clause} "
        f"ORDER BY ts DESC LIMIT :limit_n"
    )
    params["limit_n"] = limit

    try:
        result = await db.execute(text(sql), params)
        rows = [dict(r._mapping) for r in result]
    except Exception as exc:
        # Likely a missing `ts` column on a non-time-series table
        # (broker_master, industry_chain). Surface a 400 rather than
        # a generic 500 so the customer can see "this dataset has no
        # ts axis" and adjust their query.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"query failed: {exc.__class__.__name__}: {str(exc)[:200]}",
        ) from exc

    payload = DataResponse(
        status=200,
        msg="success",
        data=rows,
        metadata=DataResponseMetadata(
            dataset_code=dataset_code,
            local_table=ds.local_table,
            active_source=ds.active_source,
            last_ingest_at=ds.last_ingest_at,
            row_count=len(rows),
        ),
    )

    # Charge usage AFTER the SELECT succeeds so a failed query doesn't
    # burn the customer's row quota. `bytes_out` approximated from the
    # JSON length — exact byte count is the wrong unit anyway (compression),
    # this gives ops enough signal for capacity planning.
    bytes_out = len(payload.model_dump_json())
    await _safe_record_usage(
        db,
        api_key_id=auth.api_key.id if auth.api_key else None,
        dataset_code=dataset_code,
        endpoint=f"/data/{dataset_code}",
        row_count=len(rows),
        bytes_out=bytes_out,
        latency_ms=int((time.monotonic() - started) * 1000),
        status_code=200,
    )
    return payload


async def _safe_record_usage(db: AsyncSession, **kwargs) -> None:
    """Best-effort usage logging — a transient DB error during logging
    must NEVER tank the actual response. Caller has already produced
    the data; the log row is for billing, not for correctness."""
    try:
        await record_usage(db, **kwargs)
    except Exception:
        # Could log to stderr but don't want a busted DB to flood logs.
        # Prometheus middleware captures the request count regardless.
        pass


async def _detect_id_column(db: AsyncSession, table: str) -> str | None:
    """Pick the right identity column for a `data_id` filter.

    Looks at the table's columns once (per request) and prefers
    `symbol`, then `contract`, then `cb_id`. Returns None when none
    of those exist (e.g. market-wide aggregate tables) so the caller
    can return 400 instead of building a SELECT with an undefined
    column."""
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        result = await db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": table},
        )
        cols = {r[0] for r in result}
    else:
        # SQLite-friendly path for tests.
        result = await db.execute(text(f"PRAGMA table_info({table})"))
        cols = {r[1] for r in result}

    for candidate in ("symbol", "contract", "cb_id"):
        if candidate in cols:
            return candidate
    return None


# ── Admin endpoints ──────────────────────────────────────────────


@router.get(
    "/admin/datasets",
    response_model=list[DatasetSourceItem],
    summary="Operator listing of every dataset_sources row",
)
async def admin_list_datasets(
    db: AsyncSession = Depends(get_finmind_db),
    _admin_key: str = Depends(require_admin_key),
) -> list[DatasetSourceItem]:
    rows = (
        await db.execute(select(DatasetSource).order_by(DatasetSource.dataset_code))
    ).scalars().all()
    return [
        DatasetSourceItem.model_validate(r, from_attributes=True)
        for r in rows
    ]


_VALID_SOURCES = {"finmind", "twse", "tpex", "taifex", "mops", "tdcc"}


@router.patch(
    "/admin/datasets/{dataset_code}",
    response_model=DatasetSourceItem,
    summary="Toggle enabled / flip active_source per dataset",
)
async def admin_update_dataset(
    dataset_code: str,
    body: DatasetSourceUpdate,
    db: AsyncSession = Depends(get_finmind_db),
    _admin_key: str = Depends(require_admin_key),
) -> DatasetSourceItem:
    row = await db.get(DatasetSource, dataset_code)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown dataset: {dataset_code}",
        )
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.active_source is not None:
        if body.active_source not in _VALID_SOURCES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"active_source must be one of {sorted(_VALID_SOURCES)}; "
                    f"got '{body.active_source}'"
                ),
            )
        row.active_source = body.active_source
    await db.commit()
    await db.refresh(row)
    return DatasetSourceItem.model_validate(row, from_attributes=True)


# ── Stripe webhook ──────────────────────────────────────────────


@router.post(
    "/webhooks/stripe",
    summary="Stripe webhook receiver — verifies signature + dispatches",
    status_code=status.HTTP_200_OK,
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_finmind_db),
) -> dict[str, Any]:
    """One endpoint for all Stripe events. Returns 200 on success
    (Stripe stops retrying), 401 on signature failure (Stripe retries
    until it gives up), 500 on handler crash (Stripe retries up to 3
    days — gives ops time to fix the bug).

    Body is read as raw bytes for signature verification, then parsed
    as JSON. Reading via `request.body()` rather than the JSON body
    parser is critical: any reformatting (e.g. Pydantic re-serialization)
    breaks the HMAC.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "stripe webhook disabled — set STRIPE_WEBHOOK_SECRET "
                "to enable"
            ),
        )

    if not stripe_signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing Stripe-Signature header",
        )

    body = await request.body()
    try:
        verify_signature(body, stripe_signature, secret)
    except SignatureError as exc:
        # Don't leak which check failed — same 401 for malformed,
        # stale, mismatched, missing-secret. Avoids the endpoint
        # becoming an oracle for attackers probing the secret.
        log.warning("stripe webhook signature rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Stripe signature",
        )

    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"malformed JSON: {exc}",
        )

    outcome = await process_event(db, event)
    return {
        "event_id": outcome.event_id,
        "event_type": outcome.event_type,
        "status": outcome.status,
        "note": outcome.note,
    }


# ── Bulk export ─────────────────────────────────────────────────


_EXPORT_HARD_LIMIT = 1_000_000  # protects DB + memory regardless of plan
_EXPORT_BATCH_SIZE = 5_000      # rows per chunked yield


def _csv_escape(value: Any) -> str:
    """RFC 4180-ish: quote any value containing comma / quote / newline,
    double-up embedded quotes. Cheaper than the stdlib `csv` module
    for the streaming hot-loop and avoids buffering full rows."""
    if value is None:
        return ""
    s = str(value)
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


async def _stream_csv(result, columns: list[str]):
    """Generator yielding CSV bytes in batches. Header first, then
    `_EXPORT_BATCH_SIZE` rows per yield. Streaming to FastAPI
    StreamingResponse keeps memory flat regardless of total rows."""
    yield (",".join(columns) + "\n").encode("utf-8")

    buffer: list[str] = []
    n = 0
    for row in result:
        rec = row._mapping
        buffer.append(",".join(_csv_escape(rec.get(c)) for c in columns))
        n += 1
        if n % _EXPORT_BATCH_SIZE == 0:
            yield ("\n".join(buffer) + "\n").encode("utf-8")
            buffer = []
    if buffer:
        yield ("\n".join(buffer) + "\n").encode("utf-8")


async def _stream_jsonl(result, columns: list[str]):
    """Generator yielding NDJSON (newline-delimited JSON) bytes. One
    object per line is the most language-portable streaming format —
    pandas + Spark + jq + duckdb all parse it natively without a
    schema declaration."""
    buffer: list[str] = []
    n = 0
    for row in result:
        rec = row._mapping
        line = json.dumps(
            {c: rec.get(c) for c in columns},
            default=str,
            ensure_ascii=False,
        )
        buffer.append(line)
        n += 1
        if n % _EXPORT_BATCH_SIZE == 0:
            yield ("\n".join(buffer) + "\n").encode("utf-8")
            buffer = []
    if buffer:
        yield ("\n".join(buffer) + "\n").encode("utf-8")


@router.get(
    "/data/{dataset_code}/export",
    summary=(
        "Bulk export of a dataset's local mirror — CSV or JSONL stream"
    ),
)
async def export_data(
    dataset_code: str,
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
    data_id: str | None = Query(None, description="Filter by symbol / contract."),
    start_date: str | None = Query(None, description="YYYY-MM-DD inclusive."),
    end_date: str | None = Query(None, description="YYYY-MM-DD inclusive."),
    limit: int = Query(
        _EXPORT_HARD_LIMIT,
        le=_EXPORT_HARD_LIMIT,
        ge=1,
        description=f"Max rows to export (capped at {_EXPORT_HARD_LIMIT}).",
    ),
    db: AsyncSession = Depends(get_finmind_db),
    auth: AuthContext = Depends(require_api_key),
):
    """Bulk-friendly companion to /data/{code}: streams up to 1M rows
    as CSV (default) or NDJSON. Memory stays flat thanks to chunked
    yields — a 1M-row export uses ~10MB on the server, not 1GB.

    Quota accounting: row count + bytes_out logged AFTER the stream
    completes (best-effort). The pre-stream check_quota uses
    `calls_remaining` only — a single export = 1 call regardless of
    row count, so a customer with 100 calls/day can do 100 export
    requests as long as they fit in `quota_daily_rows`. The hard
    1M-row cap protects DB + memory from a single runaway request.
    """
    started = time.monotonic()

    # Scope gate — same rules as /data. Bulk export is the more
    # dangerous misuse path, so the check here is non-optional.
    await _authorize_dataset_scope(db, auth, dataset_code)

    quota = None
    if auth.api_key is not None:
        quota = await check_quota(db, auth.api_key)
        if not quota.permitted:
            await _safe_record_usage(
                db,
                api_key_id=auth.api_key.id,
                dataset_code=dataset_code,
                endpoint=f"/data/{dataset_code}/export",
                row_count=0,
                bytes_out=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                status_code=429,
                error_message=quota.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=quota.reason or "quota exhausted",
                headers={
                    "Retry-After": str(int(
                        (quota.reset_at - datetime.now(quota.reset_at.tzinfo))
                        .total_seconds()
                    )),
                },
            )
        # Soft cap: don't let one export exceed the customer's full
        # daily row budget. Keeps a runaway export from starving
        # subsequent requests in the same day.
        remaining_rows = max(0, quota.daily_row_limit - quota.rows_used)
        limit = min(limit, remaining_rows or limit)

    ds = await db.get(DatasetSource, dataset_code)
    if ds is None or not ds.local_table:
        await _safe_record_usage(
            db,
            api_key_id=auth.api_key.id if auth.api_key else None,
            dataset_code=dataset_code,
            endpoint=f"/data/{dataset_code}/export",
            row_count=0,
            bytes_out=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            status_code=404 if ds is None else 503,
        )
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND if ds is None
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=(
                f"unknown dataset: {dataset_code}" if ds is None
                else f"{dataset_code}: destination table not yet built"
            ),
        )

    where: list[str] = []
    params: dict[str, Any] = {}
    if data_id:
        col = await _detect_id_column(db, ds.local_table)
        if col is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{dataset_code} ({ds.local_table}) has no per-row "
                    "identity column — drop `data_id` from your query"
                ),
            )
        where.append(f"{col} = :data_id")
        params["data_id"] = data_id

    sd = _coerce_date(start_date)
    ed = _coerce_date(end_date)
    if sd:
        where.append("ts >= :start_date")
        params["start_date"] = sd
    if ed:
        where.append("ts <= :end_date")
        params["end_date"] = ed

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM {ds.local_table}{where_clause} LIMIT :limit_n"
    params["limit_n"] = limit

    # Materialize column list from the first batch for the stream
    # header. Using `result.keys()` after execute gives us the column
    # order exactly as the SELECT * returns — same order downstream
    # consumers will see in the CSV header.
    try:
        result = await db.execute(text(sql), params)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"query failed: {exc.__class__.__name__}: {str(exc)[:200]}",
        ) from exc

    columns = list(result.keys())

    # Wrap the streaming generator so we can record usage AFTER the
    # stream ends. The closure captures `db` + `auth` + `started`
    # so the post-stream record_usage has the same context the request
    # started with.
    async def _wrapped():
        rows_streamed = 0
        bytes_streamed = 0
        header_seen = False
        gen = (
            _stream_csv(result, columns)
            if format == "csv"
            else _stream_jsonl(result, columns)
        )
        try:
            async for chunk in gen:
                bytes_streamed += len(chunk)
                newlines = chunk.count(b"\n")
                if format == "csv" and not header_seen:
                    # First CSV chunk includes the header line — its
                    # newline doesn't count as a data row. Tracking
                    # header_seen separately avoids the rows_streamed-
                    # based heuristic that breaks when the entire
                    # body fits in the same chunk as the header.
                    newlines -= 1
                    header_seen = True
                rows_streamed += max(0, newlines)
                yield chunk
        finally:
            await _safe_record_usage(
                db,
                api_key_id=auth.api_key.id if auth.api_key else None,
                dataset_code=dataset_code,
                endpoint=f"/data/{dataset_code}/export",
                row_count=rows_streamed,
                bytes_out=bytes_streamed,
                latency_ms=int((time.monotonic() - started) * 1000),
                status_code=200,
            )

    media_type = "text/csv" if format == "csv" else "application/x-ndjson"
    filename = f"{dataset_code}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if quota is not None:
        headers["X-RateLimit-Limit"] = str(quota.daily_call_limit)
        headers["X-RateLimit-Remaining"] = str(max(0, quota.calls_remaining - 1))
        headers["X-RateLimit-Reset"] = str(int(quota.reset_at.timestamp()))

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        _wrapped(),
        media_type=media_type,
        headers=headers,
    )


# ── Admin: usage rollup ─────────────────────────────────────────


@router.get(
    "/admin/usage",
    summary="Per-key + per-dataset usage rollup over the last N days",
)
async def admin_usage(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_finmind_db),
    _admin_key: str = Depends(require_admin_key),
) -> dict[str, Any]:
    """Powers the AdminPage UsageCard chart. Returns three rollups:

      - `by_day`:     calls + rows per UTC day across all keys
      - `by_dataset`: calls + rows per dataset_code (last N days)
      - `by_key`:     calls + rows per api_key_id (last N days,
                      NULL key_id rolled up as `<anonymous>`)

    All three derived from the same window. Cheap — `api_usage_events`
    is a hypertable + the queries hit indexed (api_key_id, ts) /
    indexed (ts) paths."""
    from datetime import timedelta as _td

    from sqlalchemy import func as sa_func
    from sqlalchemy import text as sa_text

    from finmind.models.billing import ApiUsageEvent

    cutoff = datetime.now(tz=timezone.utc) - _td(days=days)

    by_day_rows = (
        await db.execute(
            select(
                sa_func.strftime("%Y-%m-%d", ApiUsageEvent.ts).label("day")
                if db.bind.dialect.name == "sqlite"
                else sa_func.to_char(ApiUsageEvent.ts, "YYYY-MM-DD").label("day"),
                sa_func.count().label("calls"),
                sa_func.coalesce(sa_func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(sa_text("day"))
            .order_by(sa_text("day"))
        )
    ).all()

    by_dataset_rows = (
        await db.execute(
            select(
                ApiUsageEvent.dataset_code,
                sa_func.count().label("calls"),
                sa_func.coalesce(sa_func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(ApiUsageEvent.dataset_code)
            .order_by(sa_func.count().desc())
        )
    ).all()

    by_key_rows = (
        await db.execute(
            select(
                ApiUsageEvent.api_key_id,
                sa_func.count().label("calls"),
                sa_func.coalesce(sa_func.sum(ApiUsageEvent.row_count), 0).label("rows"),
            )
            .where(ApiUsageEvent.ts >= cutoff)
            .group_by(ApiUsageEvent.api_key_id)
            .order_by(sa_func.count().desc())
        )
    ).all()

    return {
        "window_days": days,
        "by_day": [
            {"day": r[0], "calls": int(r[1]), "rows": int(r[2])}
            for r in by_day_rows
        ],
        "by_dataset": [
            {
                "dataset_code": r[0] or "<unknown>",
                "calls": int(r[1]),
                "rows": int(r[2]),
            }
            for r in by_dataset_rows
        ],
        "by_key": [
            {
                "api_key_id": r[0],
                "owner_label": (
                    f"key#{r[0]}" if r[0] is not None else "<anonymous>"
                ),
                "calls": int(r[1]),
                "rows": int(r[2]),
            }
            for r in by_key_rows
        ],
    }
