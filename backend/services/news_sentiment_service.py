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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai.llm_router import stream_chat
from cache.redis_cache import cache_delete, cache_get, cache_incr, cache_set
from config import settings
from db.session import AsyncSessionLocal
from models.corporate_announcement import CorporateAnnouncement
from models.news_article import NewsArticle
from services.llm_parsing_utils import (
    extract_json_object,
    loads_lenient,
    strip_code_fence,
    strip_think_blocks,
)

log = logging.getLogger(__name__)


# ── Per-provider failure backoff ──────────────────────────────────
# When the LLM provider returns a transient error (503, network
# blip, malformed JSON, parse failure), we want to stop hitting it
# for a while instead of burning the daily cap on certain-failure
# calls. The previous behaviour reserved one cap unit per *attempt*
# (success or fail), so a 24h provider outage = 24 cap units burned
# without any rows scored — the next morning's healthy provider
# couldn't even start until the daily reset.
#
# Two Redis keys per provider:
#   sentiment:fail_count:{provider}  integer, TTL 7d (failure window
#                                    naturally ages out after a quiet
#                                    week so a one-off blip doesn't
#                                    leave a long memory)
#   sentiment:cooldown:{provider}    marker key, TTL == backoff window;
#                                    while present, the cron skips the
#                                    provider entirely
#
# Backoff schedule (capped at 6h):
#   1 fail  → 1h cooldown
#   2 fail  → 2h cooldown
#   3 fail  → 4h cooldown
#   4+ fail → 6h cooldown
# A successful batch clears both keys, so a flapping provider that
# briefly recovers resets back to 1h on the next failure.

_COOLDOWN_KEY_PREFIX = "sentiment:cooldown:"
_FAIL_COUNT_KEY_PREFIX = "sentiment:fail_count:"
_BACKOFF_SCHEDULE_HOURS = [1, 2, 4, 6]
_FAIL_COUNT_TTL = 7 * 86400


async def _is_provider_in_cooldown(provider: str) -> bool:
    """Return True iff the provider is currently in failure cooldown.

    Fail-open on Redis outage (returns False) — without Redis we
    can't enforce the cooldown, but the daily cap counter
    (`_can_make_llm_call`) ALREADY fails closed, so runaway spend
    is already prevented. Logging here is debug because Redis
    health is observed elsewhere.
    """
    try:
        return (await cache_get(_COOLDOWN_KEY_PREFIX + provider)) is not None
    except Exception as exc:
        log.debug(
            "news_sentiment.cooldown_check_failed",
            extra={"provider": provider, "error": str(exc)},
        )
        return False


async def _record_provider_failure(provider: str, *, lane: str) -> int:
    """Bump the failure counter and set/extend the cooldown TTL
    according to `_BACKOFF_SCHEDULE_HOURS`.

    `lane` is one of "news" | "announcement" — used for the
    Prometheus counter so operators can split a news-only outage
    from an announcement-only outage. Returns the new failure
    count for log/test correlation.
    """
    try:
        count = await cache_incr(
            _FAIL_COUNT_KEY_PREFIX + provider, ttl_seconds=_FAIL_COUNT_TTL,
        )
    except Exception as exc:
        log.warning(
            "news_sentiment.fail_count_incr_failed",
            extra={"provider": provider, "error": str(exc)},
        )
        # Treat as first failure when counter unavailable — at least
        # imposes the 1h floor instead of letting the cron spin.
        count = 1

    idx = min(max(count, 1) - 1, len(_BACKOFF_SCHEDULE_HOURS) - 1)
    hours = _BACKOFF_SCHEDULE_HOURS[idx]
    try:
        await cache_set(
            _COOLDOWN_KEY_PREFIX + provider, "1", ttl_seconds=hours * 3600,
        )
    except Exception as exc:
        log.warning(
            "news_sentiment.cooldown_set_failed",
            extra={"provider": provider, "error": str(exc)},
        )

    try:
        from middleware.metrics import SENTIMENT_PROVIDER_COOLDOWN_TOTAL
        SENTIMENT_PROVIDER_COOLDOWN_TOTAL.labels(
            provider=provider, lane=lane,
        ).inc()
    except Exception:
        pass

    log.warning(
        "news_sentiment.provider_cooldown_set",
        extra={
            "provider": provider,
            "fail_count": count,
            "cooldown_hours": hours,
            "lane": lane,
        },
    )
    return count


async def _clear_provider_cooldown(provider: str) -> None:
    """Reset the failure counter + cooldown after a successful batch.

    Called on every success rather than only-on-recovery so a flapping
    provider doesn't gradually escalate to the 6h cap from incidental
    sub-hour blips.
    """
    try:
        await cache_delete(_COOLDOWN_KEY_PREFIX + provider)
        await cache_delete(_FAIL_COUNT_KEY_PREFIX + provider)
    except Exception as exc:
        log.debug(
            "news_sentiment.cooldown_clear_failed",
            extra={"provider": provider, "error": str(exc)},
        )


# ── Daily LLM-call cap (separate from per-provider cooldown) ──────
async def _can_make_llm_call(db: AsyncSession | None = None) -> bool:
    """Atomically reserve one LLM call against the daily cap.

    Returns False once the day's cap is exhausted; the caller must stop.
    The counter resets at UTC midnight via the 24h TTL set on the first
    increment of the day.

    Cap value is resolved at runtime via `runtime_config_service` so an
    admin can retune `SENTIMENT_DAILY_LLM_CALL_CAP` from the UI without
    redeploying. Falls back to the compiled-in default if the runtime
    resolver fails for any reason.

    Fails *closed* on Redis outage — without the counter we can't enforce
    the cap, so we'd rather skip a scoring pass than risk spending an
    unbounded amount during a multi-hour Redis incident.
    """
    return await _can_reserve_llm_call(
        db,
        cap_setting="SENTIMENT_DAILY_LLM_CALL_CAP",
        compiled_default=settings.SENTIMENT_DAILY_LLM_CALL_CAP,
        redis_key_prefix="sentiment:llm_calls",
    )


async def _can_make_interactive_llm_call(
    db: AsyncSession | None = None,
) -> bool:
    """Same shape as `_can_make_llm_call` but spends from a separate
    daily budget — the inline `score_specific_articles` path used by
    the backtest news auto-backfill (`services.news_backfill_service`).

    Why a separate counter: the cron and the interactive path serve
    different SLAs. A heavy day of backtests must not starve the
    hourly cron's ability to score the live RSS fire-hose, and a busy
    cron day must not block a user who picks an old backtest date.
    Two budgets, two counters, two runtime tunables.
    """
    return await _can_reserve_llm_call(
        db,
        cap_setting="SENTIMENT_INTERACTIVE_BACKFILL_CAP",
        compiled_default=settings.SENTIMENT_INTERACTIVE_BACKFILL_CAP,
        redis_key_prefix="sentiment:interactive_llm_calls",
    )


async def _can_reserve_llm_call(
    db: AsyncSession | None,
    *,
    cap_setting: str,
    compiled_default: int,
    redis_key_prefix: str,
) -> bool:
    """Shared cap-reserve mechanic. See `_can_make_llm_call` for the
    fail-closed semantics — both daily and interactive callers share
    identical Redis-incr-with-TTL behaviour, only the key prefix and
    runtime-config setting name differ.
    """
    cap = compiled_default
    if db is not None:
        try:
            from services.runtime_config_service import get_int as _get_int
            cap = await _get_int(db, cap_setting)
        except Exception as exc:
            log.warning("news_sentiment.runtime_config_failed",
                        extra={"setting": cap_setting, "error": str(exc)})
    if cap <= 0:
        return True
    today = datetime.now(UTC).strftime("%Y%m%d")
    key = f"{redis_key_prefix}:{today}"
    try:
        count = await cache_incr(key, ttl_seconds=86400)
    except Exception as exc:
        log.error("news_sentiment.cap_check_failed_fail_closed",
                  extra={"setting": cap_setting, "error": str(exc)})
        return False
    return count <= cap

_BATCH_SIZE = 20
_MAX_AGE_DAYS_FOR_SCORING = 7
# Direct-feed articles carry an extracted body (G5). We feed the model a
# lead excerpt (not the whole 20k-char body) so it can disambiguate
# headlines the title alone leaves ambiguous ("台積電法說" — beat or miss?)
# without blowing up the batch's token budget. Title-only articles
# (Google News redirect links) simply omit the `lead` field.
_BODY_EXCERPT_CHARS = 800


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
    "你是金融新聞情緒分析師。針對下列每則台股相關新聞，"
    "判斷對該股票或大盤的短線（1-5 個交易日）情緒方向。\n"
    "每則含 title（標題）；部分附 lead（前文摘要），若有請優先依 lead "
    "的實質內容判斷，標題可能為吸睛用語。\n\n"
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
    # Built with json.dumps so a title/lead containing a quote, newline or
    # backslash can't break the JSON we hand the model (the old f-string
    # build did no escaping — a `"` in a headline silently corrupted the
    # whole batch's input).
    items: list[dict] = []
    for r in rows:
        symbol = f"[{r.symbol}] " if r.symbol else "[大盤] "
        title = r.title.strip().replace("\n", " ")[:120]
        obj: dict = {"id": r.id, "title": f"{symbol}{title}"}
        if r.body:
            lead = " ".join(r.body.split())[:_BODY_EXCERPT_CHARS]
            if lead:
                obj["lead"] = lead
        items.append(obj)
    return json.dumps(items, ensure_ascii=False, indent=1)


def _parse_response(text: str) -> list[dict]:
    """Return the parsed JSON array or [] on any malformed output.

    Uses the shared lenient parser so the same `// 註解` / trailing-comma /
    full-width-brace dialects we handle for the discussion synthesizer
    don't silently brick a sentiment batch (which used to drop the entire
    20-article window when one model emitted a stray comment)."""
    cleaned = strip_code_fence(strip_think_blocks(text))
    try:
        data = loads_lenient(cleaned)
    except json.JSONDecodeError:
        salvaged = extract_json_object(cleaned) if cleaned else None
        if salvaged is not None:
            try:
                data = loads_lenient(salvaged)
            except json.JSONDecodeError:
                log.warning("news_sentiment.parse_failed", extra={"raw": cleaned[:200]})
                return []
        else:
            log.warning("news_sentiment.parse_failed", extra={"raw": cleaned[:200]})
            return []
    if not isinstance(data, list):
        log.warning("news_sentiment.unexpected_shape", extra={"type": type(data).__name__})
        return []
    return data


async def _run_llm_score(
    items_str: str,
    valid_ids: set[int],
    *,
    provider: str,
    model: str,
    db: AsyncSession,
    persona_id: str,
) -> tuple[list[_Scored] | None, str | None]:
    """Shared LLM-call core for both the news scorer (`_score_batch`)
    and the announcement scorer (`_score_announcement_batch`). Takes
    a pre-formatted items string + the set of valid IDs to filter
    the response against.

    `persona_id` is what the usage event gets recorded under so
    admins can split background-task cost between
    `_system:news_sentiment` and `_system:announcement_sentiment`
    in UsageCard.

    Two-axis result:
      - `(scored, None)` — LLM responded with parseable JSON. `scored`
        may be empty (LLM saw nothing categorisable) but the call
        itself succeeded. Caller should stamp the input rows so the
        next pass doesn't retry the same headlines.
      - `(None, error_msg)` — LLM totally failed: error event from
        the stream, exception during streaming, empty content, or
        unparseable response. Caller MUST NOT stamp the input rows
        so the next pass can retry transient failures.
    """
    messages = [
        {"role": "system", "content": "你是專業的金融新聞情緒分析助理，只輸出 JSON。"},
        {"role": "user", "content": _PROMPT_TEMPLATE.format(items=items_str)},
    ]

    assembled = ""
    usage_seen: dict[str, int] | None = None
    error_msg: str | None = None
    try:
        from services.runtime_config_service import get_int as _runtime_get_int
        max_tokens_budget = await _runtime_get_int(db, "SENTIMENT_LLM_MAX_TOKENS")
    except Exception as exc:
        log.warning("news_sentiment.runtime_config_failed",
                    extra={"setting": "SENTIMENT_LLM_MAX_TOKENS", "error": str(exc)})
        max_tokens_budget = settings.SENTIMENT_LLM_MAX_TOKENS
    try:
        async for event in stream_chat(
            messages=messages,
            provider=provider,
            model=model,
            max_tokens=max_tokens_budget,
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
                error_msg = event.get("message") or "llm error"
                log.warning(
                    "news_sentiment.llm_error",
                    extra={"llm_error": error_msg, "persona_id": persona_id},
                )
    except Exception as exc:
        log.warning(
            "news_sentiment.stream_failed",
            extra={"error": str(exc), "persona_id": persona_id},
        )
        return None, f"stream exception: {exc}"

    if usage_seen is not None:
        from services.llm_usage_service import record_usage
        await record_usage(
            db,
            user_id=None,
            provider=provider,
            model=model,
            persona_id=persona_id,
            prompt_tokens=usage_seen["prompt_tokens"],
            completion_tokens=usage_seen["completion_tokens"],
        )

    if error_msg is not None:
        return None, error_msg

    if not assembled.strip():
        return None, "llm returned no content (empty stream)"

    parsed = _parse_response(assembled)
    cleaned_check = strip_code_fence(strip_think_blocks(assembled)).strip()
    if not parsed and cleaned_check not in ("[]", ""):
        snippet = assembled.strip().replace("\n", " ")[:200]
        return None, f"llm response did not parse as JSON array; raw={snippet}"

    out: list[_Scored] = []
    for item in parsed:
        try:
            aid = int(item["id"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if aid not in valid_ids:
            continue
        score = max(-1.0, min(1.0, score))
        out.append(_Scored(article_id=aid, score=score, label=_bucket(score)))
    return out, None


async def _score_batch(
    rows: list[NewsArticle],
    *,
    provider: str,
    model: str,
    db: AsyncSession,
) -> tuple[list[_Scored] | None, str | None]:
    """News-side wrapper around `_run_llm_score`. See its docstring
    for the two-axis result contract."""
    if not rows:
        return [], None
    return await _run_llm_score(
        items_str=_format_items(rows),
        valid_ids={r.id for r in rows},
        provider=provider, model=model, db=db,
        persona_id="_system:news_sentiment",
    )


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


# ── Corporate announcement scoring (PR-D1b) ──────────────────────
#
# Mirrors the news-side functions above but operates on
# `corporate_announcements`. The shared LLM core (`_run_llm_score`)
# means the prompt + parse + clamp + filter logic stays single-
# sourced — only the format string + I/O bindings are
# announcement-specific.
#
# Cost: shared daily cap with news (`_can_make_llm_call` reads the
# same Redis counter). At 100 calls/day default cap and ~50-150
# MOPS rows per day, announcements typically consume 3-8 batches
# per day, leaving plenty of headroom for news.


def _format_announcement_items(rows: list[CorporateAnnouncement]) -> str:
    """Same JSON-array shape as `_format_items` but includes the
    announcement category in the title — material-info LLM scoring
    is more accurate when the model sees `(category)` framing
    alongside the disclosure title (a "減資" disclosure with a
    neutral-sounding title still warrants a bearish prior, etc.)."""
    lines = []
    for r in rows:
        symbol = f"[{r.symbol}] " if r.symbol else ""
        category = f"({r.category}) " if r.category else ""
        title = r.title.strip().replace("\n", " ")[:120]
        lines.append(f'  {{"id": {r.id}, "title": "{symbol}{category}{title}"}}')
    return "[\n" + ",\n".join(lines) + "\n]"


async def _fetch_unscored_announcements(
    db: AsyncSession, *, limit: int, max_age_days: int,
) -> list[CorporateAnnouncement]:
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stmt = (
        select(CorporateAnnouncement)
        .where(
            CorporateAnnouncement.sentiment_scored_at.is_(None),
            CorporateAnnouncement.announced_at >= cutoff,
        )
        .order_by(CorporateAnnouncement.announced_at.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


async def _score_announcement_batch(
    rows: list[CorporateAnnouncement],
    *,
    provider: str,
    model: str,
    db: AsyncSession,
) -> tuple[list[_Scored] | None, str | None]:
    """Announcement-side wrapper around `_run_llm_score`. Records
    usage under `_system:announcement_sentiment` so admins can split
    cost between the two background-task lanes in UsageCard."""
    if not rows:
        return [], None
    return await _run_llm_score(
        items_str=_format_announcement_items(rows),
        valid_ids={r.id for r in rows},
        provider=provider, model=model, db=db,
        persona_id="_system:announcement_sentiment",
    )


async def _write_announcement_scores(
    db: AsyncSession, scored: list[_Scored], *, fallback_ids: list[int],
) -> int:
    """Mirror of `_write_scores` for `corporate_announcements`. Same
    fallback-stamp behaviour: every input row gets `sentiment_
    scored_at` updated even when the LLM didn't return for it, so
    the next pass doesn't keep retrying the same un-scoreable
    headlines."""
    now = datetime.now(UTC)
    by_id = {s.article_id: s for s in scored}
    written = 0
    for aid in fallback_ids:
        s = by_id.get(aid)
        if s is None:
            stmt = (
                update(CorporateAnnouncement)
                .where(CorporateAnnouncement.id == aid)
                .values(sentiment_scored_at=now)
            )
        else:
            stmt = (
                update(CorporateAnnouncement)
                .where(CorporateAnnouncement.id == aid)
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


async def score_pending_announcements(
    *,
    db: AsyncSession | None = None,
    batch_size: int = _BATCH_SIZE,
    max_batches: int = 4,
    provider: str | None = None,
    model: str | None = None,
    max_age_days: int | None = None,
) -> dict[str, int]:
    """Mirror of `score_pending` for `corporate_announcements`.

    Sharing the same LLM provider/model defaults as news scoring
    (resolved via the `news_sentiment` system task config) — keeps
    operators from having to dial in two separate model selections
    when both lanes are fundamentally doing the same task.

    Daily cap is shared via `_can_make_llm_call` against the same
    Redis counter so the 100/day default protects the operator's
    budget across BOTH lanes.

    Returns the same counter shape as `score_pending` so the cron
    aggregator can render combined status uniformly.
    """
    own_session = db is None
    session = db if db is not None else AsyncSessionLocal()
    if max_age_days is None:
        try:
            from services.runtime_config_service import (
                get_int as _runtime_get_int,
            )
            max_age_days = await _runtime_get_int(
                session, "SENTIMENT_CRON_MAX_AGE_DAYS",
            )
        except Exception as exc:
            log.warning(
                "announcement_sentiment.runtime_max_age_failed",
                extra={"error": str(exc)},
            )
            max_age_days = settings.SENTIMENT_CRON_MAX_AGE_DAYS
    considered = 0
    scored_count = 0
    batches_run = 0
    cap_hit = False
    last_error: str | None = None
    try:
        if provider is None or model is None:
            from services.system_task_config_service import resolve as _resolve_task
            r_provider, r_model = await _resolve_task(session, "news_sentiment")
            provider = provider or r_provider
            model = model or r_model

        # Skip the entire run when the provider is in failure
        # cooldown — saves the daily cap units the previous code
        # would burn ramming a known-down provider once per hour.
        if await _is_provider_in_cooldown(provider):
            log.info(
                "announcement_sentiment.provider_in_cooldown",
                extra={"provider": provider},
            )
            return {
                "considered": 0, "scored": 0, "batches": 0,
                "cap_hit": 0, "cooldown_active": 1,
                "provider": provider,
            }
        for _ in range(max_batches):
            if not await _can_make_llm_call(session):
                cap_hit = True
                log.warning(
                    "announcement_sentiment.daily_cap_hit",
                    extra={"cap": settings.SENTIMENT_DAILY_LLM_CALL_CAP},
                )
                break
            rows = await _fetch_unscored_announcements(
                session, limit=batch_size, max_age_days=max_age_days,
            )
            if not rows:
                break
            scored, batch_error = await _score_announcement_batch(
                rows, provider=provider, model=model, db=session,
            )
            if scored is None:
                last_error = batch_error
                await _record_provider_failure(provider, lane="announcement")
                log.warning(
                    "announcement_sentiment.batch_failed_skipping_stamp",
                    extra={"error": batch_error, "rows": len(rows)},
                )
                break
            ids = [r.id for r in rows]
            written = await _write_announcement_scores(
                session, scored, fallback_ids=ids,
            )
            considered += len(rows)
            scored_count += written
            batches_run += 1
            # First successful batch clears the cooldown so a
            # provider that briefly flapped doesn't carry stale
            # backoff state into the next hour.
            if batches_run == 1:
                await _clear_provider_cooldown(provider)
            if not scored:
                break
        result: dict[str, int | str] = {
            "considered": considered,
            "scored": scored_count,
            "batches": batches_run,
            "cap_hit": int(cap_hit),
        }
        if last_error:
            result["error"] = last_error
        return result
    finally:
        if own_session:
            await session.close()


async def score_pending(
    *,
    db: AsyncSession | None = None,
    batch_size: int = _BATCH_SIZE,
    max_batches: int = 4,
    provider: str | None = None,
    model: str | None = None,
    max_age_days: int | None = None,
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

    `max_age_days=None` resolves at runtime from
    ``SENTIMENT_CRON_MAX_AGE_DAYS`` (default 7). Cranking this up via
    AdminPage RuntimeTunablesCard is the operator's lever for draining
    a fresh FinMind backfill whose articles' ``published_at`` falls
    outside the default 7-day window — without it, those backfilled
    rows sit unscored forever (only inline scoring during round-button
    presses ever touches them, ~100 rows × N rounds, leaving 87K+ rows
    permanently unreached on a typical backfill).

    Tests pass an explicit value to bypass the runtime resolver.
    """
    own_session = db is None
    session = db if db is not None else AsyncSessionLocal()
    # Resolve max_age_days from runtime config when caller didn't
    # pass it explicitly. Done up-front (before the per-batch loop)
    # so the lookup hits the 60s Redis cache once instead of per
    # batch.
    if max_age_days is None:
        try:
            from services.runtime_config_service import (
                get_int as _runtime_get_int,
            )
            max_age_days = await _runtime_get_int(
                session, "SENTIMENT_CRON_MAX_AGE_DAYS",
            )
        except Exception as exc:
            log.warning(
                "news_sentiment.runtime_max_age_failed",
                extra={"error": str(exc)},
            )
            max_age_days = settings.SENTIMENT_CRON_MAX_AGE_DAYS
    considered = 0
    scored_count = 0
    batches_run = 0
    cap_hit = False
    last_error: str | None = None
    try:
        if provider is None or model is None:
            from services.system_task_config_service import resolve as _resolve_task
            r_provider, r_model = await _resolve_task(session, "news_sentiment")
            provider = provider or r_provider
            model = model or r_model

        # Skip the entire run when the provider is in failure
        # cooldown — saves the daily cap units the previous code
        # would burn ramming a known-down provider once per hour.
        if await _is_provider_in_cooldown(provider):
            log.info(
                "news_sentiment.provider_in_cooldown",
                extra={"provider": provider},
            )
            return {
                "considered": 0, "scored": 0, "batches": 0,
                "cap_hit": 0, "cooldown_active": 1,
                "provider": provider,
            }
        for _ in range(max_batches):
            if not await _can_make_llm_call(session):
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
            scored, batch_error = await _score_batch(
                rows, provider=provider, model=model, db=session,
            )
            if scored is None:
                # LLM totally failed (error / empty / parse). Don't
                # stamp the rows — let the next pass retry transient
                # failures instead of permanently marking 20 rows as
                # un-scoreable. Break to avoid burning the rest of
                # the cap on the same failure mode.
                last_error = batch_error
                await _record_provider_failure(provider, lane="news")
                log.warning(
                    "news_sentiment.batch_failed_skipping_stamp",
                    extra={"error": batch_error, "rows": len(rows)},
                )
                break
            ids = [r.id for r in rows]
            written = await _write_scores(session, scored, fallback_ids=ids)
            considered += len(rows)
            scored_count += written
            batches_run += 1
            # First successful batch clears the cooldown so a
            # provider that briefly flapped doesn't carry stale
            # backoff state into the next hour.
            if batches_run == 1:
                await _clear_provider_cooldown(provider)
            if not scored:
                # LLM responded with parseable but empty result —
                # break out instead of burning the remaining batches
                # on the same failure mode.
                break
        result: dict[str, int | str] = {
            "considered": considered,
            "scored": scored_count,
            "batches": batches_run,
            "cap_hit": int(cap_hit),
        }
        if last_error:
            result["error"] = last_error
        return result
    finally:
        if own_session:
            await session.close()


async def score_specific_articles(
    db: AsyncSession,
    *,
    article_ids: list[int],
    batch_size: int = _BATCH_SIZE,
    provider: str | None = None,
    model: str | None = None,
    persona_id_for_usage: str = "_system:news_sentiment_interactive",
) -> dict[str, int]:
    """Score a fixed list of article IDs synchronously, blocking until
    every batch finishes or the interactive cap is hit.

    Distinct from `score_pending` (the hourly cron) in three ways:
      - Scope is a fixed ID list, not "any unscored row in window".
      - Cap is `SENTIMENT_INTERACTIVE_BACKFILL_CAP`, with its own
        Redis counter so a busy cron day doesn't block a user backtest
        and a heavy backtest day doesn't starve the cron.
      - Usage is recorded under `_system:news_sentiment_interactive`
        so the AdminPage UsageCard distinguishes interactive cost from
        background cron cost.

    Used by the backtest news auto-backfill: after `insert_news_articles`
    persists rows pulled from FinMind, this function runs a few LLM
    batches against them so the discussion round that triggered the
    backfill reads SCORED data instead of NULL-filtered nothing.

    Returns the same shape as `score_pending`:
      `{"considered": int, "scored": int, "batches": int, "cap_hit": int}`

    `article_ids` is the candidate set; rows already scored (race with
    cron) are silently dropped. `batch_size` matches the cron path.
    `provider`/`model` default to the `news_sentiment` system task
    config so admin overrides apply automatically.
    """
    if not article_ids:
        return {"considered": 0, "scored": 0, "batches": 0, "cap_hit": 0}

    if provider is None or model is None:
        from services.system_task_config_service import resolve as _resolve_task
        r_provider, r_model = await _resolve_task(db, "news_sentiment")
        provider = provider or r_provider
        model = model or r_model

    considered = 0
    scored_count = 0
    batches_run = 0
    cap_hit = False
    last_error: str | None = None

    remaining = list(article_ids)
    while remaining:
        if not await _can_make_interactive_llm_call(db):
            cap_hit = True
            log.warning(
                "news_sentiment.interactive_cap_hit",
                extra={
                    "remaining": len(remaining),
                    "scored_so_far": scored_count,
                },
            )
            break

        chunk_ids = remaining[:batch_size]
        remaining = remaining[batch_size:]

        # Re-fetch fresh — the cron may have scored some of these
        # between when the caller built the ID list and now. Skip
        # any row that already has a score so we don't double-spend
        # the interactive budget.
        stmt = (
            select(NewsArticle)
            .where(
                NewsArticle.id.in_(chunk_ids),
                NewsArticle.sentiment_scored_at.is_(None),
            )
        )
        rows = list((await db.scalars(stmt)).all())
        if not rows:
            continue

        scored, batch_error = await _score_batch(
            rows, provider=provider, model=model, db=db,
        )
        if scored is None:
            # LLM totally failed — preserve rows for retry on next
            # press (don't stamp them as tried-but-failed forever)
            # and surface the error message so the discussion ctx
            # shows the user WHY scoring is returning 0.
            last_error = batch_error
            log.warning(
                "news_sentiment.interactive_batch_failed",
                extra={
                    "error": batch_error,
                    "remaining": len(remaining),
                    "scored_so_far": scored_count,
                },
            )
            break
        ids = [r.id for r in rows]
        written = await _write_scores(db, scored, fallback_ids=ids)
        considered += len(rows)
        scored_count += written
        batches_run += 1

        if not scored:
            # LLM responded with parseable but empty result — break
            # so we don't burn remaining cap on the same failure mode.
            break

    result: dict[str, int | str] = {
        "considered": considered,
        "scored": scored_count,
        "batches": batches_run,
        "cap_hit": int(cap_hit),
    }
    if last_error:
        result["error"] = last_error
    return result


async def select_unscored_in_window(
    db: AsyncSession,
    *,
    market: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[int]:
    """Return up to `limit` article IDs in [start, end] for `market`
    that still have NULL `sentiment_scored_at`. Used by
    `news_backfill_service` to pick the just-backfilled rows to feed
    into `score_specific_articles` — picking by window instead of
    capturing IDs from the insert avoids the dialect-specific
    RETURNING handling and tolerates partial-overlap re-runs.

    Ordered newest-first so when the cap is hit we score the freshest
    articles (which is what the discussion round cares about most).
    """
    stmt = (
        select(NewsArticle.id)
        .where(
            NewsArticle.market == market,
            NewsArticle.published_at >= start,
            NewsArticle.published_at <= end,
            NewsArticle.sentiment_scored_at.is_(None),
        )
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


async def read_recent_market_sentiment(
    db: AsyncSession,
    *,
    market: str = "TW",
    limit: int = 50,
    max_age_hours: int = 48,
    as_of: datetime | None = None,
) -> dict:
    """Aggregate sentiment-scored news for the discussion orchestrator
    to inject as context.

    Pulls **all** sentiment-scored news for the market, not just
    `symbol IS NULL` rows. The earlier filter was theoretically clean
    (per-symbol sentiment is not the same as market sentiment) but in
    practice broke the feature: Google News TW headlines almost always
    include a 4-6 digit stock code, so `ingest_news_tw`'s symbol-tagger
    set `symbol` on ~99% of rows, leaving the market-wide aggregator
    with nothing to read. Each headline still carries its `symbol`
    field so the persona can tell `2330 法說` from a broad-market
    headline; the LLM does the disambiguation, not the SQL filter.

    Aggregation vs sample split (PR #214): `avg_score` and the
    bullish/bearish/neutral counts are computed over **every** scored
    article in the time window, not just the headlines slice. The
    earlier code averaged over the same `limit=20` rows it returned —
    a 200-article-bullish day still showed "20 articles, 14 bullish"
    so personas saw a sample-biased ratio. Now `headlines` stays
    capped at `limit` (token budget) but the counts reflect the full
    population the personas can trust.

    Returns:
        {
          "avg_score": float,            # over full window
          "bullish": int,                # over full window
          "bearish": int,                # over full window
          "neutral": int,                # over full window
          "considered": int,             # full-window article count
          "headlines": [{"title", "symbol", "score", "label",
                         "published_at"}],   # capped at `limit`
        }
    """
    # Backtest mode (PR #224): when `as_of` is set, anchor the
    # window to that timestamp so news from after `as_of` doesn't
    # leak in. Live mode keeps `now()` as the anchor.
    anchor = as_of or datetime.now(UTC)
    cutoff = anchor - timedelta(hours=max_age_hours)
    base_filter = [
        NewsArticle.market == market,
        NewsArticle.published_at >= cutoff,
        NewsArticle.published_at <= anchor,
        NewsArticle.sentiment_score.isnot(None),
    ]
    # PR #261: sentiment is scored asynchronously by the hourly cron,
    # so an article published at as_of-1h might not be scored until
    # as_of+24h. Including those rows in a backtest read at as_of
    # leaks the post-as_of scoring decision. Restrict to articles
    # whose `sentiment_scored_at <= anchor` to simulate "what the
    # persona would have seen at as_of". Live mode skips this — the
    # row's sentiment_score being non-null already implies it was
    # scored before "now".
    if as_of is not None:
        base_filter.append(NewsArticle.sentiment_scored_at <= anchor)

    # ── full-window aggregate ──────────────────────────────────────
    # Pull ALL rows in the window (no limit) for an unbiased average +
    # bucket counts. The window is bounded by `max_age_hours`, so this
    # never blows up — TW news ingest produces O(100) scored rows in
    # a typical 48h window.
    agg_stmt = select(NewsArticle).where(*base_filter)
    all_rows = list((await db.scalars(agg_stmt)).all())
    considered = len(all_rows)
    if considered == 0:
        return {
            "avg_score":  0.0,
            "bullish":    0,
            "bearish":    0,
            "neutral":    0,
            "considered": 0,
            "headlines":  [],
        }

    total = sum(r.sentiment_score or 0.0 for r in all_rows)
    avg = total / considered
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for r in all_rows:
        counts[r.sentiment_label or "neutral"] = (
            counts.get(r.sentiment_label or "neutral", 0) + 1
        )

    # ── headline sample (capped) ───────────────────────────────────
    # Within the tight market window, prefer articles that carry an
    # extracted body (direct-publisher feeds): their sentiment was scored
    # WITH the full text (G5 R8-3) so it's more reliable than a title-only
    # Google-News score. `body IS NOT NULL` first, then newest — the
    # window is short (~48h) so this costs little recency. `has_fulltext`
    # is surfaced so the synthesizer can weight body-backed items higher.
    head_stmt = (
        select(NewsArticle)
        .where(*base_filter)
        .order_by(
            NewsArticle.body.is_(None).asc(),  # False(0)=has body → first
            NewsArticle.published_at.desc(),
        )
        .limit(limit)
    )
    head_rows = list((await db.scalars(head_stmt)).all())

    return {
        "avg_score":  round(avg, 3),
        "bullish":    counts["bullish"],
        "bearish":    counts["bearish"],
        "neutral":    counts["neutral"],
        "considered": considered,
        "headlines": [
            {
                "title":        r.title,
                "symbol":       r.symbol,
                "score":        r.sentiment_score,
                "label":        r.sentiment_label,
                "published_at": r.published_at.isoformat(),
                "has_fulltext": r.body is not None,
            }
            for r in head_rows
        ],
    }


async def read_symbol_sentiment(
    db: AsyncSession,
    *,
    market: str,
    symbol: str,
    limit: int = 10,
    max_age_hours: int = 168,
    as_of: datetime | None = None,
) -> dict | None:
    """Aggregate sentiment-scored news for a specific symbol over the last
    `max_age_hours`. Returns None if no scored articles exist for the
    symbol (so the caller can omit the per-symbol block instead of
    rendering an empty stub).

    Window default bumped to 168h / 7d (PR #214): per-symbol news is
    much sparser than market-wide news. A 72h window often returned
    None for mid-cap names that get one mention every few days, so
    personas saw "no news" for stocks that had genuine recent
    coverage. 7 days is short enough that the signal is still
    actionable.

    Aggregation is over the **full window**, not the limited
    headlines slice — same fix as `read_recent_market_sentiment`. A
    stock with 30 scored articles, 25 of them bullish, no longer
    looks like "10 articles, 7 bullish" just because the headlines
    cap stops at 10.
    """
    # Backtest mode (PR #224): same anchor pattern as the
    # market-wide reader.
    anchor = as_of or datetime.now(UTC)
    cutoff = anchor - timedelta(hours=max_age_hours)
    base_filter = [
        NewsArticle.market == market,
        NewsArticle.symbol == symbol,
        NewsArticle.published_at >= cutoff,
        NewsArticle.published_at <= anchor,
        NewsArticle.sentiment_score.isnot(None),
    ]
    # PR #261: same async-scoring leak fix as the market-wide reader.
    if as_of is not None:
        base_filter.append(NewsArticle.sentiment_scored_at <= anchor)

    agg_stmt = select(NewsArticle).where(*base_filter)
    all_rows = list((await db.scalars(agg_stmt)).all())
    if not all_rows:
        return None
    considered = len(all_rows)
    total = sum(r.sentiment_score or 0.0 for r in all_rows)
    avg = total / considered
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for r in all_rows:
        label = r.sentiment_label or "neutral"
        counts[label] = counts.get(label, 0) + 1

    head_stmt = (
        select(NewsArticle)
        .where(*base_filter)
        .order_by(NewsArticle.published_at.desc())
        .limit(limit)
    )
    head_rows = list((await db.scalars(head_stmt)).all())

    return {
        "symbol":     symbol,
        "considered": considered,
        "count":      considered,   # retained for backward-compat callers
        "avg_score":  round(avg, 3),
        "bullish":    counts["bullish"],
        "bearish":    counts["bearish"],
        "neutral":    counts["neutral"],
        "headlines": [
            {
                "title":        r.title,
                "score":        r.sentiment_score,
                "label":        r.sentiment_label,
                "published_at": r.published_at.isoformat(),
                # Per-symbol news is sparse; keep pure recency ordering
                # (staleness matters more here than at market scale), but
                # still flag body-backed items so the reader knows which
                # sentiment was scored with full text.
                "has_fulltext": r.body is not None,
            }
            for r in head_rows
        ],
    }
