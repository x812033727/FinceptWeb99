"""
B1 個股 AI 研究報告 API.

POST /api/ai/stock-report/{market}/{symbol}  – SSE streaming report (auth)
GET  /api/ai/stock-reports                   – list caller's saved reports
GET  /api/ai/stock-reports/{report_id}       – fetch one saved report

Quota: shares the daily AI counter with /api/ai/chat (viewer 5/day,
analyst 20/day, admin exempt) via the same `_check_quota` /
`_refund_quota` helpers — one report generation costs one AI request.
Provider API-key resolution is likewise identical: `stream_chat`
receives `db` + `user_id`, so per-user DB keys → system DB keys →
.env fallback in that order.

SSE event shapes (superset of the /chat contract — the frontend's
existing readers ignore unknown keys):
  data: {"stage": "context" | "generating"}      progress milestone
  data: {"delta": "..."}                          report token
  data: {"error": "..."}                          fatal error
  data: {"done": {"report_id": "...", "created_at": "..."}}  persisted
  data: [DONE]                                    terminator
"""
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.llm_router import default_model_for, stream_chat
from api.ai_agents.router import _check_quota, _refund_quota
from auth.permissions import require_viewer
from config import settings
from db.session import get_db
from models.stock_report import StockReport

logger = logging.getLogger(__name__)
router = APIRouter()
CurrentUser = Annotated[dict, Depends(require_viewer)]

_SUPPORTED_MARKETS = ("TW", "US")
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")
# Providers with no configured key don't error — they yield a single
# "[<Provider> API key not configured]" delta (or an error event for
# the OpenAI-compat providers). Detect the delta variant so we don't
# archive a "report" whose body is that placeholder.
_MISSING_KEY_RE = re.compile(r"API key not configured", re.IGNORECASE)

# A full six-section report needs far more room than chat's 1024
# default; ~800-1200 zh-TW chars plus markdown scaffolding.
_REPORT_MAX_TOKENS = 4096
_REPORT_TEMPERATURE = 0.4
_PREVIEW_CHARS = 120


# ── schemas ───────────────────────────────────────────────────────

class StockReportGenerateRequest(BaseModel):
    provider: str | None = None   # override default provider
    model: str | None = None      # override provider's default model


class StockReportSummary(BaseModel):
    id: uuid.UUID
    symbol: str
    market: str
    model: str
    created_at: datetime
    preview: str


class StockReportDetail(StockReportSummary):
    content_md: str
    sections: dict[str, str] | None = None


def _preview(content: str) -> str:
    """First non-heading text of the report, flattened to one line."""
    lines = [
        ln.strip() for ln in content.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    flat = " ".join(lines)
    return flat[:_PREVIEW_CHARS]


def _summary(row: StockReport) -> StockReportSummary:
    return StockReportSummary(
        id=row.id, symbol=row.symbol, market=row.market,
        model=row.model or "", created_at=row.created_at,
        preview=_preview(row.content_md),
    )


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


# ── routes ────────────────────────────────────────────────────────

@router.post("/stock-report/{market}/{symbol}")
async def generate_stock_report(
    market: str,
    symbol: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: StockReportGenerateRequest | None = None,
):
    """Stream a structured zh-TW analyst report for one symbol.

    Context is assembled through the discussion subsystem's block
    builders (see `services/stock_report_service.py`), a single LLM
    call streams the report as SSE deltas, and the finished markdown
    is archived to `stock_reports`.
    """
    market = market.upper()
    symbol = symbol.upper()
    if market not in _SUPPORTED_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"market must be one of {list(_SUPPORTED_MARKETS)}",
        )
    if not _SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")

    await _check_quota(user, db)

    req = body or StockReportGenerateRequest()
    provider = (req.provider or settings.DEFAULT_LLM_PROVIDER).lower()
    model = req.model or default_model_for(provider)

    async def event_generator() -> AsyncGenerator[bytes, None]:
        from services.llm_usage_service import record_usage
        from services.stock_report_service import (
            assemble_report_context,
            build_report_messages,
            persist_report,
        )

        produced_content = False
        usage_seen: dict[str, int] | None = None
        parts: list[str] = []
        try:
            # Context gathering can take seconds (news sentiment /
            # FinMind waterfalls) — surface a stage event so the
            # panel can show "整理市場資料中" instead of going silent.
            yield _sse({"stage": "context"})
            ctx = await assemble_report_context(
                db, market=market, symbol=symbol,
            )
            messages = build_report_messages(
                ctx, market=market, symbol=symbol,
            )
            yield _sse({"stage": "generating"})

            async for event in stream_chat(
                messages=messages,
                provider=provider,
                model=model,
                max_tokens=_REPORT_MAX_TOKENS,
                temperature=_REPORT_TEMPERATURE,
                db=db,
                user_id=user["id"],
            ):
                etype = event.get("type")
                if etype == "usage":
                    usage_seen = {
                        "prompt_tokens": int(event.get("prompt_tokens", 0)),
                        "completion_tokens": int(event.get("completion_tokens", 0)),
                    }
                    continue
                if etype == "delta":
                    text = event.get("text", "")
                    parts.append(text)
                    produced_content = True
                    yield _sse({"delta": text})
                elif etype == "error":
                    yield _sse({"error": event.get("message", "unknown error")})

            content = "".join(parts).strip()
            if produced_content and _MISSING_KEY_RE.search(content) and len(content) < 200:
                # The provider streamed only its "key not configured"
                # placeholder — treat as failure: no archive row, and
                # the quota is refunded below.
                produced_content = False
                yield _sse({"error": content.strip("[]").strip()})
            elif produced_content and content:
                report = await persist_report(
                    db,
                    user_id=uuid.UUID(user["id"]),
                    symbol=symbol,
                    market=market,
                    content_md=content,
                    model=model,
                )
                yield _sse({"done": {
                    "report_id": str(report.id),
                    "created_at": report.created_at.isoformat(),
                }})
        except Exception as exc:  # noqa: BLE001 — must surface as SSE, not a broken pipe
            logger.error(
                "stock_report.stream_failed",
                extra={"symbol": symbol, "market": market, "error": str(exc)},
                exc_info=True,
            )
            yield _sse({"error": str(exc)})
        finally:
            if not produced_content:
                await _refund_quota(user)
            if usage_seen is not None and produced_content:
                await record_usage(
                    db,
                    user_id=user["id"],
                    provider=provider,
                    model=model,
                    persona_id="stock_report",
                    prompt_tokens=usage_seen["prompt_tokens"],
                    completion_tokens=usage_seen["completion_tokens"],
                )
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stock-reports", response_model=list[StockReportSummary])
async def list_stock_reports(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    symbol: str | None = Query(default=None),
    market: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
):
    """List the caller's saved reports, newest first."""
    stmt = select(StockReport).where(
        StockReport.user_id == uuid.UUID(user["id"]),
    )
    if symbol:
        stmt = stmt.where(StockReport.symbol == symbol.upper())
    if market:
        stmt = stmt.where(StockReport.market == market.upper())
    stmt = stmt.order_by(StockReport.created_at.desc()).limit(limit)
    rows = (await db.scalars(stmt)).all()
    return [_summary(r) for r in rows]


@router.get("/stock-reports/{report_id}", response_model=StockReportDetail)
async def get_stock_report(
    report_id: uuid.UUID,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Fetch one saved report. 404 for other users' reports — the
    response doesn't distinguish "not yours" from "doesn't exist"."""
    row = await db.get(StockReport, report_id)
    if row is None or row.user_id != uuid.UUID(user["id"]):
        raise HTTPException(status_code=404, detail="Report not found")
    return StockReportDetail(
        id=row.id, symbol=row.symbol, market=row.market,
        model=row.model or "", created_at=row.created_at,
        preview=_preview(row.content_md),
        content_md=row.content_md,
        sections=row.sections,
    )
