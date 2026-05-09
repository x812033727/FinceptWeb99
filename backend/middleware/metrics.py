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
