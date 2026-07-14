import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { IngestHealthSparkline } from "./IngestHealthSparkline";
import { DataTable, type DataTableColumn } from "../ui/table";

interface IngestHealth {
  job_id: string;
  last_run_at: string | null;
  ok: boolean;
  row_count: number;
  error: string | null;
  // Optional: present when the upstream returned HTTP 200 with
  // body.status != 200 (FinMind paywall / tier-unavailable). Distinct
  // from `error` because the JSON body's raw msg is preserved here so
  // the UI can render it without parsing the prefix back out.
  silent_deny?: string | null;
  // Optional: ISO date/datetime of the latest `ts` actually written
  // by this run. Older than 2 trading days vs `last_run_at` triggers
  // an amber "stale data" pill.
  latest_data_ts?: string | null;
  // Backend-computed flag (since the trading-day check needs server-
  // side calendar logic). When the field is present the frontend
  // trusts it as-is; falls back to a client-side calendar-day check
  // for older deploys whose serializer didn't emit this field.
  data_stale?: boolean;
}

interface IngestRetryResult {
  status: string;
  message: string;
}

/**
 * Decide the status badge for one ingest row.
 *
 * Six states (in priority order):
 *  - **pending**: never-run-yet (`last_run_at === null`). Newly-
 *    deployed cron before its first scheduled tick — neutral grey,
 *    not a failure.
 *  - **ok**: last run succeeded (`r.ok === true`). Green.
 *  - **queued**: error message starts with `queued:` — written by
 *    the admin "Retry now" endpoint while the background task is
 *    in-flight. Blue, so the operator sees their click registered
 *    without a misleading red badge.
 *  - **silent deny**: error starts with `silent_paywall:` (or the
 *    structured `silent_deny` field is set). The upstream returned
 *    HTTP 200 with body.status != 200 — typically FinMind paywall /
 *    quota burst. Purple, distinct from `skipped` so operators can
 *    spot upstream-tier issues at a glance.
 *  - **skipped**: last run failed but the error message starts with
 *    `skipped:` — a known-permanent-state record from a fail-soft
 *    path (e.g. FinMind explicit 4xx paywall, or a deliberately-off
 *    cron). Yellow, not red, so it doesn't read as an actionable
 *    incident.
 *  - **error**: last run failed for any other reason. Red.
 *
 * Pulled out of the JSX so it can be unit-tested without mounting
 * the whole React tree + TanStack Query provider.
 */
export function deriveIngestBadge(r: IngestHealth): { text: string; cls: string } {
  const neverRun = r.last_run_at === null;
  if (neverRun) {
    return {
      text: "pending",
      cls: "bg-muted/30 text-muted-foreground border border-border",
    };
  }
  if (r.ok) {
    return {
      text: "ok",
      cls: "bg-success/10 text-success border border-success/30",
    };
  }
  const errLower = r.error?.toLowerCase() ?? "";
  if (errLower.startsWith("queued")) {
    return {
      text: "queued",
      cls: "bg-info/10 text-info border border-info/30",
    };
  }
  if (r.silent_deny || errLower.startsWith("silent_paywall")) {
    return {
      text: "silent deny",
      cls: "bg-purple-500/10 text-purple-400 border border-purple-500/30",
    };
  }
  if (errLower.startsWith("skipped")) {
    return {
      text: "skipped",
      cls: "bg-warning/10 text-warning border border-warning/30",
    };
  }
  return {
    text: "error",
    cls: "bg-danger/10 text-danger border border-danger/30",
  };
}


// 2 calendar days. Long enough to absorb weekends + a holiday Monday
// without nagging, short enough to flag a stale ingest before personas
// quote a week-old number. Per-task crons run daily, so >48h between
// `last_run_at` and `latest_data_ts` is real drift.
const STALE_DATA_MS = 2 * 24 * 3600 * 1000;

/**
 * True iff `latest_data_ts` is older than `last_run_at` by more than
 * the staleness budget. Returns false when either field is missing —
 * tasks that don't write time-series data (verify, score, prune)
 * legitimately have `latest_data_ts === null` and shouldn't render
 * a stale badge.
 *
 * Prefers the backend-computed `data_stale` field, which uses
 * trading-day arithmetic (Mon-Fri). Falls back to a calendar-day
 * check for older deploys where the field is absent. The fallback
 * has the known false-positive over weekends — operators upgrading
 * past this commit get the corrected behaviour.
 */
export function isDataStale(r: IngestHealth): boolean {
  if (typeof r.data_stale === "boolean") return r.data_stale;
  if (!r.latest_data_ts || !r.last_run_at) return false;
  const dataAt = new Date(r.latest_data_ts).getTime();
  const runAt = new Date(r.last_run_at).getTime();
  if (!Number.isFinite(dataAt) || !Number.isFinite(runAt)) return false;
  return runAt - dataAt > STALE_DATA_MS;
}

// Mirror of `RETRYABLE_INGEST_JOBS` in `backend/api/admin/router.py`.
// Adding a job here without registering it backend-side just makes the
// button render — the POST will 404. Keep the two sides in sync.
const RETRYABLE_INGEST_JOBS = new Set([
  "ingest_news_tw",
  "ingest_news_international",
  "ingest_news_feeds",
  "enrich_news_fulltext",
  "ingest_ohlcv_tw",
  "ingest_institutional_tw",
  "ingest_margin_tw",
  "ingest_revenue_tw",
  "ingest_revenue_tw_slow",
  "ingest_buyback_tw",
  "ingest_govt_bank_flow_tw",
  "ingest_taiex_history",
  "ingest_taiex_tr_history",
  "ingest_risk_signals_tw",
  "ingest_holdings_aggregates_tw",
  "score_discussion_outcomes",
  "score_news_sentiment",
]);

// Jobs that have a localized description + schedule label. The strings
// themselves live in the i18n locale files under
// `admin.ingestHealth.jobs.<slug>` (slug = job_id with ':' → '_'), so the
// table renders in the operator's selected language. `schedule` is
// mirrored from the corresponding `add_job` call in
// `backend/tasks/scheduler.py` — when you change the cron expression
// there, update the matching locale entry or the displayed schedule will
// silently drift out of sync.
//
// The `newsfeed:*` entries are per-source health rows emitted by
// ingest_news_feeds — one per direct publisher feed so a silently-dead
// feed is visible instead of hidden behind a job-level "ok". Not
// individually retryable (see the parent ingest_news_feeds job), so no
// entry in RETRYABLE_INGEST_JOBS.
const JOB_META_IDS = new Set<string>([
  "ingest_news_tw",
  "ingest_news_international",
  "ingest_news_feeds",
  "enrich_news_fulltext",
  "newsfeed:cnyes",
  "newsfeed:udn_money",
  "newsfeed:ltn_ec",
  "newsfeed:cna_finance",
  "ingest_ohlcv_tw",
  "ingest_fundamentals_tw",
  "ingest_institutional_tw",
  "ingest_margin_tw",
  "ingest_taiex_history",
  "ingest_taiex_tr_history",
  "ingest_risk_signals_tw",
  "ingest_holdings_aggregates_tw",
  "ingest_revenue_tw",
  "ingest_revenue_tw_slow",
  "ingest_buyback_tw",
  "ingest_govt_bank_flow_tw",
  "ingest_quotes_retention_tw",
  "score_news_sentiment",
  "auto_run_discussion",
  "verify_discussion_outcome",
  "score_discussion_outcomes",
]);

// i18n key slug for a job_id (':' is the i18next namespace separator,
// so it must be sanitized out of the key path).
const jobMetaKey = (jobId: string, field: "desc" | "sched") =>
  `admin.ingestHealth.jobs.${jobId.replace(/:/g, "_")}.${field}`;

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "—";
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.round(diffSec / 3600)}h ago`;
  return `${Math.round(diffSec / 86400)}d ago`;
}

interface SchedulerHeartbeat {
  last_beat_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  version: string | null;
  ttl_seconds: number;
}

/**
 * Decide the scheduler-heartbeat badge.
 *
 *   - `stale=true` + `last_beat_at=null` → red "scheduler dead": Redis
 *     key is missing entirely (TTL expired or never written). Use
 *     phrasing that points the operator at "is the process running?"
 *     not "did Redis hiccup?".
 *   - `stale=true` with a `last_beat_at` → amber "scheduler stale": the
 *     scheduler beat at some point but >60s ago. Likely event-loop
 *     wedge, not a full process death.
 *   - `stale=false` → green with the age in seconds.
 */
export function deriveSchedulerBadge(
  hb: SchedulerHeartbeat | undefined,
): { text: string; cls: string; tooltip: string } {
  if (!hb) {
    return {
      text: "loading",
      cls: "bg-muted/30 text-muted-foreground border border-border",
      tooltip: "Querying /admin/scheduler/health…",
    };
  }
  if (hb.stale && hb.last_beat_at === null) {
    return {
      text: "scheduler dead",
      cls: "bg-danger/10 text-danger border border-danger/30",
      tooltip:
        `No heartbeat in Redis. APScheduler may have crashed — ` +
        `check pod logs and confirm the FastAPI lifespan started ` +
        `setup_jobs(). TTL: ${hb.ttl_seconds}s.`,
    };
  }
  if (hb.stale) {
    return {
      text: `scheduler stale (${Math.round(hb.age_seconds ?? 0)}s)`,
      cls: "bg-warning/10 text-warning border border-warning/30",
      tooltip:
        `Last heartbeat ${Math.round(hb.age_seconds ?? 0)}s ago — ` +
        `expected every 30s. Event loop may be wedged on a slow ` +
        `LLM call or a long DB transaction. Version: ${hb.version ?? "?"}.`,
    };
  }
  return {
    text: `scheduler ok (${Math.round(hb.age_seconds ?? 0)}s)`,
    cls: "bg-success/10 text-success border border-success/30",
    tooltip:
      `Heartbeat ${Math.round(hb.age_seconds ?? 0)}s ago — within the ` +
      `60s freshness budget. Version: ${hb.version ?? "?"}.`,
  };
}

export function IngestHealthCard() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { open, toggle } = useCollapsible("admin.ingest-health");
  const { data: serverRows = [], isLoading } = useQuery<IngestHealth[]>({
    queryKey: ["admin", "ingest-health"],
    queryFn: () => api.get("/admin/ingest/health").then((r) => r.data),
    refetchInterval: 60_000,
  });
  // Heartbeat polled at 15s — needs to be tighter than the row poll
  // (60s) so a scheduler death is surfaced quickly. Independent
  // queryKey so stale ingest data doesn't make heartbeat refetch
  // unnecessarily, and vice-versa.
  const { data: heartbeat } = useQuery<SchedulerHeartbeat>({
    queryKey: ["admin", "scheduler-health"],
    queryFn: () => api.get("/admin/scheduler/health").then((r) => r.data),
    refetchInterval: 15_000,
  });
  // Union with the whitelist so newly-deployed jobs that haven't
  // hit their first cron tick yet still appear in the table — admin
  // can fire the first run manually via "Retry now" instead of
  // waiting for 09:30 UTC for the row to materialise. Placeholder
  // rows have `last_run_at=null` which renders as "—" via timeAgo().
  const rows: IngestHealth[] = (() => {
    const seen = new Set(serverRows.map((r) => r.job_id));
    const placeholders: IngestHealth[] = [];
    for (const jobId of RETRYABLE_INGEST_JOBS) {
      if (!seen.has(jobId)) {
        placeholders.push({
          job_id: jobId,
          last_run_at: null,
          ok: false,
          row_count: 0,
          error: null,
        });
      }
    }
    return [...serverRows, ...placeholders];
  })();
  const retry = useMutation({
    mutationFn: (jobId: string) =>
      api.post<IngestRetryResult>(`/admin/ingest/${jobId}/retry`).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "ingest-health"] }),
  });
  const retryingJobId = retry.isPending ? retry.variables : null;

  const columns: DataTableColumn<IngestHealth>[] = [
    {
      key: "job",
      header: "Job",
      cellClassName: "align-top",
      render: (r) => {
        const hasMeta = JOB_META_IDS.has(r.job_id);
        return (
          <div>
            <div className="font-mono">{r.job_id}</div>
            {hasMeta && (
              <div className="text-micro text-muted-foreground/80 mt-0.5">
                {t(jobMetaKey(r.job_id, "desc"))}
              </div>
            )}
          </div>
        );
      },
    },
    {
      key: "schedule",
      header: t("admin.ingestHealth.schedule"),
      cellClassName: "align-top text-muted-foreground whitespace-nowrap",
      render: (r) => (JOB_META_IDS.has(r.job_id) ? t(jobMetaKey(r.job_id, "sched")) : "—"),
    },
    {
      key: "status",
      header: "Status",
      cellClassName: "align-top",
      render: (r) => {
        const { text: badgeText, cls: badgeCls } = deriveIngestBadge(r);
        return (
          <span className={`px-1.5 py-0.5 rounded text-micro ${badgeCls}`}>
            {badgeText}
          </span>
        );
      },
    },
    {
      key: "sparkline",
      header: <span title={t("admin.ingestHealth.sparkline_7d_title")}>7d</span>,
      cellClassName: "align-top",
      render: (r) => <IngestHealthSparkline jobId={r.job_id} />,
    },
    {
      key: "row_count",
      header: "Rows",
      numeric: true,
      cellClassName: "align-top",
      render: (r) => r.row_count.toLocaleString(),
    },
    {
      key: "last_run",
      header: "Last Run",
      cellClassName: "align-top text-muted-foreground",
      render: (r) => (
        <div>
          <div>{timeAgo(r.last_run_at)}</div>
          {isDataStale(r) && (
            <span
              title={t("admin.ingestHealth.data_stale_title", { ts: r.latest_data_ts })}
              className="inline-block mt-0.5 px-1 py-0.5 rounded text-[9px] bg-warning/10 text-warning border border-warning/30"
            >
              data stale
            </span>
          )}
        </div>
      ),
    },
    {
      key: "error",
      header: "Error",
      cellClassName: "align-top text-muted-foreground",
      render: (r) => (
        <span
          className="block truncate max-w-[24rem]"
          title={r.error ?? undefined}
        >
          {r.error ?? ""}
        </span>
      ),
    },
    {
      key: "action",
      header: "Action",
      align: "right",
      cellClassName: "align-top",
      render: (r) =>
        RETRYABLE_INGEST_JOBS.has(r.job_id) ? (
          <button
            type="button"
            disabled={retry.isPending}
            onClick={() => retry.mutate(r.job_id)}
            title="Clear backoff and queue one immediate run"
            className="px-2 py-1 rounded border border-border bg-background hover:bg-accent/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {retryingJobId === r.job_id ? "Retrying..." : "Retry now"}
          </button>
        ) : (
          <span className="text-muted-foreground">-</span>
        ),
    },
  ];

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title="Scheduled Ingest Health"
        headerRight={
          <div className="flex items-center gap-2">
            {(() => {
              const sched = deriveSchedulerBadge(heartbeat);
              return (
                <span
                  className={`px-1.5 py-0.5 rounded text-micro ${sched.cls}`}
                  title={sched.tooltip}
                >
                  {sched.text}
                </span>
              );
            })()}
            <span className="text-micro text-muted-foreground">
              refreshes every 60s
            </span>
          </div>
        }
      />
      {open && (<>
      {retry.isSuccess && (
        <p className="text-xs text-success">{retry.data.message}</p>
      )}
      {retry.isError && (
        <p className="text-xs text-danger">Retry request failed. Please try again.</p>
      )}

      {isLoading ? (
        <p className="text-xs text-muted-foreground animate-pulse">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No ingest jobs have reported yet — first cron tick is pending.
        </p>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.job_id}
          mobileMode="scroll"
          aria-label="Scheduled Ingest Health"
        />
      )}
      </>)}
    </div>
  );
}
