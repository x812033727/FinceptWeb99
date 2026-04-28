"""News sentiment scoring.

Periodically picks up news articles with NULL `sentiment_score` and asks
an LLM to label each as bullish / bearish / neutral with a numeric score
in [-1, +1]. Results are written back to the same row so the discussion
orchestrator can use sentiment-weighted news context without re-querying
the LLM.

Design choices:
  - Batch up to `_BATCH_SIZE` headlines per LLM call to keep cost down
    (one prompt scores 20 headlines instead of 20 separate prompts).
  - Use Anthropic Claude Haiku as the default scorer — fast, cheap,
    Chinese-aware. Override via the `provider` / `model` arguments if the
    deployment doesn't have an Anthropic key.
  - Parse model output as JSON. If the model returns malformed JSON we
    log + skip (sentiment_scored_at stays NULL) so the next pass retries.
  - Same dialect dispatch as the rest of the ingest layer so SQLite
    test runs and Postgres prod both work without branching at the call
    site.

Sentiment buckets:
  score >= 0.25 → "bullish"
  score <= -0.25 → "bearish"
  otherwise     → "neutral"
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai.llm_router import stream_chat
from cache.redis_cache import cache_incr
from config import settings
from db.session import AsyncSessionLocal
from models.news_article import NewsArticle

log = logging.getLogger(__name__)


async def _can_make_llm_call() -> bool:
    """Atomically reserve one LLM call against the daily cap.

    Returns False once the day's cap is exhausted; the caller must stop.
    The counter resets at UTC midnight via the 24h TTL set on the first
    increment of the day. Redis outage falls open (returns True) — we
    prefer occasional over-spend to halting the scorer entirely.
    """
    cap = settings.SENTIMENT_DAILY_LLM_CALL_CAP
    if cap <= 0:
        return True
    today = datetime.now(UTC).strftime("%Y%m%d")
    key = f"sentiment:llm_calls:{today}"
    try:
        count = await cache_incr(key, ttl_seconds=86400)
    except Exception as exc:
        log.warning("news_sentiment.cap_check_failed", extra={"error": str(exc)})
        return True
    return count <= cap

_BATCH_SIZE = 20
_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_MAX_AGE_DAYS_FOR_SCORING = 7


def _bucket(score: float) -> str:
    if score >= 0.25:
        return "bullish"
    if score <= -0.25:
        return "bearish"
    return "neutral"


@dataclass(frozen=True)
class _Scored:
    article_id: int
    score: float
    label: str


_PROMPT_TEMPLATE = (
    "你是金融新聞情緒分析師。針對下列每則台股相關新聞標題，"
    "判斷對該股票或大盤的短線（1-5 個交易日）情緒方向。\n\n"
    "輸出必須是合法 JSON 陣列，格式：\n"
    '[{{"id": 整數, "score": -1.0~1.0, "reason": "≤20字"}}]\n\n'
    "score 規則：\n"
    "  +1.0 = 強烈利多 (重大營收成長、訂單、政策利多)\n"
    "  +0.5 = 偏多 (法人加碼、產業展望佳)\n"
    "   0.0 = 中性 (純資訊、人事異動)\n"
    "  -0.5 = 偏空 (法人減碼、競爭加劇)\n"
    "  -1.0 = 強烈利空 (重大虧損、訴訟、下市)\n\n"
    "新聞列表：\n{items}\n\n"
    "只輸出 JSON 陣列，不要包 markdown code fence，不要前後加文字。"
)


def _format_items(rows: list[NewsArticle]) -> str:
    lines = []
    for r in rows:
        symbol = f"[{r.symbol}] " if r.symbol else "[大盤] "
        title = r.title.strip().replace("\n", " ")[:120]
        lines.append(f'  {{"id": {r.id}, "title": "{symbol}{title}"}}')
    return "[\n" + ",\n".join(lines) + "\n]"


def _strip_code_fence(text: str) -> str:
    """LLMs sometimes wrap JSON in ```json ... ``` despite being told not to."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def _parse_response(text: str) -> list[dict]:
    """Return the parsed JSON array or [] on any malformed output."""
    cleaned = _strip_code_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("news_sentiment.parse_failed", extra={"raw": cleaned[:200]})
        return []
    if not isinstance(data, list):
        log.warning("news_sentiment.unexpected_shape", extra={"type": type(data).__name__})
        return []
    return data


async def _score_batch(
    rows: list[NewsArticle],
    *,
    provider: str,
    model: str,
    db: AsyncSession,
) -> list[_Scored]:
    """One LLM call per batch. Returns parsed Scored rows; missing /
    malformed entries are silently dropped — the next pass retries.

    Token usage is recorded against `persona_id="_system:news_sentiment"`
    so admins can see background-task cost in UsageCard alongside chat
    usage.
    """
    if not rows:
        return []

    prompt = _PROMPT_TEMPLATE.format(items=_format_items(rows))
    messages = [
        {"role": "system", "content": "你是專業的金融新聞情緒分析助理，只輸出 JSON。"},
        {"role": "user", "content": prompt},
    ]

    assembled = ""
    usage_seen: dict[str, int] | None = None
    try:
        async for event in stream_chat(
            messages=messages,
            provider=provider,
            model=model,
            max_tokens=2048,
            temperature=0.0,
            db=db,
        ):
            etype = event.get("type")
            if etype == "delta":
                assembled += event.get("text", "")
            elif etype == "usage":
                usage_seen = {
                    "prompt_tokens": int(event.get("prompt_tokens", 0)),
                    "completion_tokens": int(event.get("completion_tokens", 0)),
                }
            elif etype == "error":
                log.warning(
                    "news_sentiment.llm_error",
                    extra={"message": event.get("message")},
                )
                return []
    except Exception as exc:
        log.warning("news_sentiment.stream_failed", extra={"error": str(exc)})
        return []

    if usage_seen is not None:
        from services.llm_usage_service import record_usage
        await record_usage(
            db,
            user_id=None,
            provider=provider,
            model=model,
            persona_id="_system:news_sentiment",
            prompt_tokens=usage_seen["prompt_tokens"],
            completion_tokens=usage_seen["completion_tokens"],
        )

    parsed = _parse_response(assembled)
    by_id = {r.id: r for r in rows}
    out: list[_Scored] = []
    for item in parsed:
        try:
            aid = int(item["id"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if aid not in by_id:
            continue
        score = max(-1.0, min(1.0, score))
        out.append(_Scored(article_id=aid, score=score, label=_bucket(score)))
    return out


async def _fetch_unscored(
    db: AsyncSession, *, limit: int, max_age_days: int,
) -> list[NewsArticle]:
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(NewsArticle)
        .where(
            NewsArticle.sentiment_scored_at.is_(None),
            NewsArticle.published_at >= cutoff,
        )
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


async def _write_scores(
    db: AsyncSession, scored: list[_Scored], *, fallback_ids: list[int],
) -> int:
    """Persist scores and stamp sentiment_scored_at for every input article
    (including those the LLM didn't return for) so the next pass doesn't
    keep retrying the same un-scoreable headlines.
    """
    now = datetime.now(UTC)
    by_id = {s.article_id: s for s in scored}
    written = 0
    for aid in fallback_ids:
        s = by_id.get(aid)
        if s is None:
            stmt = (
                update(NewsArticle)
                .where(NewsArticle.id == aid)
                .values(sentiment_scored_at=now)
            )
        else:
            stmt = (
                update(NewsArticle)
                .where(NewsArticle.id == aid)
                .values(
                    sentiment_score=s.score,
                    sentiment_label=s.label,
                    sentiment_scored_at=now,
                )
            )
            written += 1
        await db.execute(stmt)
    await db.commit()
    return written


async def score_pending(
    *,
    db: AsyncSession | None = None,
    batch_size: int = _BATCH_SIZE,
    max_batches: int = 4,
    provider: str | None = None,
    model: str | None = None,
    max_age_days: int = _MAX_AGE_DAYS_FOR_SCORING,
) -> dict[str, int]:
    """Score up to `batch_size * max_batches` unscored articles.

    Returns counters for the scheduler health snapshot:
      {"considered": int, "scored": int, "batches": int}

    `db` is optional — when None we open a session per call so the task
    file doesn't have to thread one through.

    `provider` / `model` default to whatever the admin selected via the
    `news_sentiment` system task config (falling back to the compiled-in
    spec when no override exists). Pass explicit values only in tests
    that want to bypass the resolver.
    """
    own_session = db is None
    session = db if db is not None else AsyncSessionLocal()
    considered = 0
    scored_count = 0
    batches_run = 0
    cap_hit = False
    try:
        if provider is None or model is None:
            from services.system_task_config_service import resolve as _resolve_task
            r_provider, r_model = await _resolve_task(session, "news_sentiment")
            provider = provider or r_provider
            model = model or r_model
        for _ in range(max_batches):
            if not await _can_make_llm_call():
                cap_hit = True
                log.warning(
                    "news_sentiment.daily_cap_hit",
                    extra={"cap": settings.SENTIMENT_DAILY_LLM_CALL_CAP},
                )
                break
            rows = await _fetch_unscored(
                session, limit=batch_size, max_age_days=max_age_days,
            )
            if not rows:
                break
            scored = await _score_batch(
                rows, provider=provider, model=model, db=session,
            )
            ids = [r.id for r in rows]
            written = await _write_scores(session, scored, fallback_ids=ids)
            considered += len(rows)
            scored_count += written
            batches_run += 1
            if not scored:
                # LLM produced nothing usable — break out instead of burning
                # the remaining batches on the same failure mode.
                break
        return {
            "considered": considered,
            "scored": scored_count,
            "batches": batches_run,
            "cap_hit": int(cap_hit),
        }
    finally:
        if own_session:
            await session.close()


async def read_recent_market_sentiment(
    db: AsyncSession,
    *,
    market: str = "TW",
    limit: int = 20,
    max_age_hours: int = 48,
) -> dict:
    """Aggregate sentiment-scored market-wide news for the discussion
    orchestrator to inject as context.

    Returns:
        {
          "avg_score": float,
          "bullish": int, "bearish": int, "neutral": int,
          "headlines": [{"title", "score", "label", "published_at"}],
        }
    """
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    stmt = (
        select(NewsArticle)
        .where(
            NewsArticle.market == market,
            NewsArticle.symbol.is_(None),   # market-wide news only — averaging
                                            # per-symbol news into "market sentiment"
                                            # is a category error
            NewsArticle.published_at >= cutoff,
            NewsArticle.sentiment_score.isnot(None),
        )
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    rows = list((await db.scalars(stmt)).all())
    if not rows:
        return {
            "avg_score": 0.0,
            "bullish": 0,
            "bearish": 0,
            "neutral": 0,
            "headlines": [],
        }
    total = sum(r.sentiment_score or 0.0 for r in rows)
    avg = total / len(rows)
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for r in rows:
        counts[r.sentiment_label or "neutral"] = counts.get(r.sentiment_label or "neutral", 0) + 1
    return {
        "avg_score": round(avg, 3),
        "bullish": counts["bullish"],
        "bearish": counts["bearish"],
        "neutral": counts["neutral"],
        "headlines": [
            {
                "title": r.title,
                "symbol": r.symbol,
                "score": r.sentiment_score,
                "label": r.sentiment_label,
                "published_at": r.published_at.isoformat(),
            }
            for r in rows
        ],
    }


async def read_symbol_sentiment(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    limit: int = 10,
    max_age_hours: int = 72,
) -> dict | None:
    """Aggregate sentiment-scored news for a specific symbol over the last
    `max_age_hours`. Returns None if no scored articles exist for the
    symbol (so the caller can omit the per-symbol block instead of
    rendering an empty stub).
    """
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    stmt = (
        select(NewsArticle)
        .where(
            NewsArticle.market == market,
            NewsArticle.symbol == symbol,
            NewsArticle.published_at >= cutoff,
            NewsArticle.sentiment_score.isnot(None),
        )
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    rows = list((await db.scalars(stmt)).all())
    if not rows:
        return None
    total = sum(r.sentiment_score or 0.0 for r in rows)
    avg = total / len(rows)
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for r in rows:
        label = r.sentiment_label or "neutral"
        counts[label] = counts.get(label, 0) + 1
    return {
        "symbol": symbol,
        "count": len(rows),
        "avg_score": round(avg, 3),
        "bullish": counts["bullish"],
        "bearish": counts["bearish"],
        "neutral": counts["neutral"],
        "headlines": [
            {
                "title": r.title,
                "score": r.sentiment_score,
                "label": r.sentiment_label,
                "published_at": r.published_at.isoformat(),
            }
            for r in rows
        ],
    }
