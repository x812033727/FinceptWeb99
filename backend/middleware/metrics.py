"""
Prometheus metrics middleware.

Exports:
  http_requests_total{method, path, status}   Counter
  http_request_duration_seconds{method, path}  Histogram
  http_active_requests                         Gauge
"""
import re
import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
ACTIVE_REQUESTS = Gauge("http_active_requests", "Currently active HTTP requests")

# Discussion learning loop counters. `post_mortem_skipped_total` /
# `_ran_total` together let operators reason about the win-rate of
# recommendations (skip rate close to 0 ⇒ threshold too tight; close
# to 1 ⇒ too loose). `lessons_*` track the feedback loop's health.
POST_MORTEM_SKIPPED_TOTAL = Counter(
    "post_mortem_skipped_total",
    "Backtest discussions whose post-mortem was skipped because the "
    "recommendation already cleared the win threshold.",
    ["market"],
)
POST_MORTEM_RAN_TOTAL = Counter(
    "post_mortem_ran_total",
    "Backtest discussions whose post-mortem self-critique was actually "
    "fired (recommendation missed the win threshold).",
    ["market"],
)
LESSONS_PERSISTED_TOTAL = Counter(
    "lessons_persisted_total",
    "Lessons written into discussion_lessons after a post-mortem "
    "synthesizer pass.",
    ["market", "category"],
)
LESSONS_INJECTED_TOTAL = Counter(
    "lessons_injected_total",
    "Lessons surfaced inside gather_market_context. `scope=market` "
    "rolls up the market-wide bucket; `scope=per_symbol` rolls up "
    "the focus-symbol buckets.",
    ["market", "scope"],
)
LESSONS_DEDUP_SKIPPED_TOTAL = Counter(
    "lessons_dedup_skipped_total",
    "Lessons rejected at write time because an identical "
    "lesson_text was persisted within the dedup window.",
    ["market"],
)
LESSON_EMBEDDINGS_TOTAL = Counter(
    "lesson_embeddings_total",
    "Lesson semantic-embedding outcomes from the inline write path "
    "and the backfill cron. `success` = vector persisted; "
    "`skipped` = embedding disabled or no API key resolved; "
    "`failed` = provider returned an error or malformed payload.",
    ["outcome"],
)

# ── Service-layer Redis cache (instrumented in cache.redis_cache) ──
# `endpoint` is the `{market}.{datatype}` label derived from the cache
# key's first two colon-delimited segments (e.g. `tw.quote`,
# `us.financials`, `crypto.news`). Keeps the label-set bounded — every
# new builder adds at most one new `endpoint` value rather than one
# per symbol, so cardinality stays in the low tens regardless of the
# universe size. Useful for resourcing TTL decisions: an endpoint
# with miss_rate ~ 0 % can probably shorten its TTL; one near 100 %
# is wasting Redis traffic.
CACHE_HITS_TOTAL = Counter(
    "cache_hits_total",
    "Redis cache_get_json hits, labelled by `{market}.{datatype}`.",
    ["endpoint"],
)
CACHE_MISSES_TOTAL = Counter(
    "cache_misses_total",
    "Redis cache_get_json misses. Counts the empty-key case AND a "
    "malformed JSON payload — both surface as `None` to the caller "
    "and trigger an upstream refetch, so the operator's view of "
    '"how often did we refetch" should treat them identically.',
    ["endpoint"],
)

# ── Market-data waterfall tier failures (C1-2) ─────────────────────
# Bumped at every upstream-tier failure site in the market services
# (tw_market_service, us_market_service) so operators can see the
# per-tier failure rate alongside the existing `log.warning("X.tier_
# failed", ...)` lines. Lets alerting distinguish a Polygon outage
# from a yfinance one without grepping logs, and surfaces the
# leading edge of an outage before the all-sources-failed event
# (which fires only after every tier in the chain has tripped).
#
# `tier="all"` is reserved for the terminal "every tier in the
# waterfall exhausted" log line — useful as its own alert so the
# operator sees a one-line summary of "no data shipped at all" as
# distinct from "one tier degraded, fallback took over".
WATERFALL_TIER_FAILED_TOTAL = Counter(
    "waterfall_tier_failed_total",
    "Market-data upstream tier failures. `datatype` ∈ "
    "{quote, history, fundamentals, financials, options, screener}; "
    "`tier` ∈ {polygon, yfinance, stooq, finnhub, twse, finmind, "
    "mis, all}.",
    ["market", "datatype", "tier"],
)

# ── Walk-forward orchestrator (PR-A1 + post-merge audit) ──────────

WALK_FORWARD_RUNS_TOTAL = Counter(
    "walk_forward_runs_total",
    "Walk-forward orchestrator runs by terminal status. "
    "`success` = every fold's train+test pair finished without "
    "raising. `partial` = at least one fold errored but others "
    "completed. `failed` = top-level exception escaped past the "
    "per-fold try/except (orchestrator infrastructure failure, "
    "should be near zero in steady state).",
    ["status"],
)
WALK_FORWARD_FOLDS_TOTAL = Counter(
    "walk_forward_folds_total",
    "Individual (train, test) folds executed. `outcome` is "
    "`completed` (both halves ran without exception) or "
    "`failed` (the per-fold try/except caught something).",
    ["outcome"],
)

# ── Scheduled ingest tasks ────────────────────────────────────────
# Wired inside `services.ingest.repository.record_health` so every
# task that already calls record_health gets instrumentation for
# free. `outcome` derived from (ok, error prefix, silent_deny):
#   ok=True                              → "ok"
#   error startswith "skipped"|"queued"  → "skipped"
#   silent_deny is not None              → "silent_deny"
#   otherwise ok=False                   → "failed"
INGEST_RUNS_TOTAL = Counter(
    "ingest_runs_total",
    "Scheduled ingest task runs by terminal outcome.",
    ["job_id", "outcome"],
)
INGEST_ROWS_WRITTEN_TOTAL = Counter(
    "ingest_rows_written_total",
    "Rows successfully upserted by scheduled ingest tasks.",
    ["job_id"],
)
INGEST_SILENT_DENY_TOTAL = Counter(
    "ingest_silent_deny_total",
    "Ingest tasks that detected upstream silent-deny "
    "(HTTP 200 + body.status != 200, e.g. FinMind paywall).",
    ["job_id", "source"],
)
INGEST_DATA_FRESHNESS_SECONDS = Gauge(
    "ingest_data_freshness_seconds",
    "Age (now - max(latest_data_ts)) of the most recent data each "
    "ingest task has written. Unset when the task did not report a ts.",
    ["job_id"],
)

# ── APScheduler liveness probe ────────────────────────────────────
# A 30s heartbeat task writes a Redis key; this gauge reflects the
# read-side freshness so operators can alert on a silent scheduler
# death. `-1` is the sentinel for "no heartbeat key found at all"
# (TTL expired or never written) — alerting rules can match either
# `> 60` (stale) or `== -1` (fully dead).
SCHEDULER_HEARTBEAT_AGE_SECONDS = Gauge(
    "scheduler_heartbeat_age_seconds",
    "Seconds since APScheduler's heartbeat task last wrote its "
    "Redis key. -1 means the key is missing (scheduler dead longer "
    "than the heartbeat TTL or never started).",
)

# ── TWSE token bucket rate limiter ────────────────────────────────
# Counts degradation events in `data/tw/twse_connector._wait_for_token`.
# Operators alerting on this can distinguish:
#   reason="redis_unavailable"  → Redis bucket unreachable; falling
#                                  back to local Semaphore + 1.1s
#                                  pacing for THIS process only.
#                                  Cross-pod coordination is lost
#                                  until Redis returns.
#   reason="bucket_starvation"  → Waited > _MAX_WAIT (30s) without
#                                  acquiring a token. Falls back to
#                                  local pacing rather than spamming
#                                  TWSE; the signal is "global TWSE
#                                  pressure is sustained".
TWSE_RATE_LIMIT_DEGRADED_TOTAL = Counter(
    "twse_rate_limit_degraded_total",
    "TWSE token-bucket fall-open events. `redis_unavailable` = Redis "
    "raised; the connector kept the request flowing via local "
    "Semaphore + 1.1s sleep. `bucket_starvation` = waited the full "
    "_MAX_WAIT without acquiring a token; same fallback applies. "
    "A non-zero rate of either is a leading indicator of TWSE 429s.",
    ["reason"],
)

# ── News sentiment provider cooldown ──────────────────────────────
# Bumped every time `_record_provider_failure` engages a per-provider
# failure backoff (1h → 6h capped). Operators can alert on a sustained
# rate to detect a provider outage that the daily-cap counter alone
# wouldn't surface — the new backoff means a flat rate of 1/hour
# during an outage stops at the first failure instead of spinning
# 24 cap-burning attempts per day.
SENTIMENT_PROVIDER_COOLDOWN_TOTAL = Counter(
    "sentiment_provider_cooldown_total",
    "News-sentiment scorer provider cooldowns triggered. `lane` "
    "splits a news-only outage from an announcement-only outage so "
    "an MOPS-side LLM glitch doesn't get blamed on the news pipeline.",
    ["provider", "lane"],
)

# ── WebSocket market-data pipeline ────────────────────────────────
# Connection gauge counts *authenticated* sockets only (pre-auth
# sockets are closed within AUTH_TIMEOUT and never register). The
# dispatch histogram times one pubsub message's full fan-out across
# all subscribed connections — the number to watch when connection
# count grows, since dispatch is currently O(connections) per tick.
WS_CONNECTIONS = Gauge(
    "ws_connections",
    "Currently connected, authenticated market WebSocket clients.",
)
WS_PUBSUB_DISPATCH_SECONDS = Histogram(
    "ws_pubsub_dispatch_seconds",
    "Wall time to fan one Redis pubsub market update out to every "
    "subscribed WebSocket connection.",
    buckets=[0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0],
)
WS_PUBSUB_RECONNECTS_TOTAL = Counter(
    "ws_pubsub_reconnects_total",
    "Times the market pubsub listener lost its Redis subscription and "
    "re-entered the reconnect/backoff loop. A sustained rate means "
    "Redis is flapping; before this counter existed a single drop "
    "silently killed all WebSocket deltas until restart.",
)

# ── APScheduler job runtimes ──────────────────────────────────────
# Wired via listener in tasks.scheduler_metrics (no per-job changes).
# Duration is measured submitted→executed; with max_instances=1 on
# every job the job_id keying is unambiguous. Jobs sharing the event
# loop with request serving means a fat bucket here (us_screener_warm
# regularly walks Stooq for ~20 s) directly explains p95 latency
# jitter on the same worker.
SCHEDULER_JOB_DURATION_SECONDS = Histogram(
    "scheduler_job_duration_seconds",
    "APScheduler job wall time from submission to completion.",
    ["job_id"],
    buckets=[0.05, 0.25, 1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0],
)
SCHEDULER_JOB_RUNS_TOTAL = Counter(
    "scheduler_job_runs_total",
    "APScheduler job executions by terminal outcome "
    "(ok / error / missed).",
    ["job_id", "outcome"],
)

# ── Core Web Vitals (reported by the frontend) ────────────────────
# LCP / INP / FCP / TTFB are time metrics in seconds; bucket
# boundaries are tuned around Google's "Good / Needs improvement /
# Poor" thresholds (e.g. LCP < 2.5 s "good", < 4 s "needs work").
WEB_VITAL_SECONDS = Histogram(
    "web_vital_seconds",
    "Core Web Vitals time metrics reported by the browser",
    ["name", "path"],
    buckets=[0.05, 0.1, 0.2, 0.5, 1.0, 1.8, 2.5, 4.0, 6.0, 10.0],
)
# CLS is a unitless cumulative-shift score; "good" < 0.1, "poor" > 0.25.
WEB_VITAL_SCORE = Histogram(
    "web_vital_score",
    "Core Web Vitals unitless score metrics reported by the browser",
    ["name", "path"],
    buckets=[0.01, 0.05, 0.1, 0.15, 0.25, 0.5, 1.0, 2.0],
)

# Normalize dynamic path segments to limit cardinality
_NORMALIZATIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/api/(us|tw)/quote/[^/]+$"),       r"/api/\1/quote/{symbol}"),
    (re.compile(r"^/api/(us|tw)/history/[^/]+$"),     r"/api/\1/history/{symbol}"),
    (re.compile(r"^/api/(us|tw)/fundamentals/[^/]+"), r"/api/\1/fundamentals/{symbol}"),
    (re.compile(r"^/api/watchlist/[^/]+/items/[^/]+"), "/api/watchlist/{wid}/items/{iid}"),
    (re.compile(r"^/api/watchlist/[^/]+/items"),       "/api/watchlist/{wid}/items"),
    (re.compile(r"^/api/watchlist/[^/]+$"),            "/api/watchlist/{wid}"),
    (re.compile(r"^/api/alerts/[^/]+$"),               "/api/alerts/{id}"),
    (re.compile(r"^/api/portfolio/[^/]+$"),            "/api/portfolio/{id}"),
]


def _normalize_path(path: str) -> str:
    for pattern, replacement in _NORMALIZATIONS:
        if pattern.match(path):
            return pattern.sub(replacement, path)
    return path


class PrometheusMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        ACTIVE_REQUESTS.inc()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            ACTIVE_REQUESTS.dec()

        latency = time.perf_counter() - start
        path = _normalize_path(request.url.path)
        REQUEST_COUNT.labels(request.method, path, response.status_code).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(latency)
        return response


async def metrics_endpoint(_request: Request) -> Response:
    """Prometheus scrape endpoint — mount at /metrics."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
