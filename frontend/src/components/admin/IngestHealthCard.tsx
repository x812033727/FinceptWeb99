import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";

interface IngestHealth {
  job_id: string;
  last_run_at: string | null;
  ok: boolean;
  row_count: number;
  error: string | null;
}

interface IngestRetryResult {
  status: string;
  message: string;
}

/**
 * Decide the status badge for one ingest row.
 *
 * Four states (in priority order):
 *  - **pending**: never-run-yet (`last_run_at === null`). Newly-
 *    deployed cron before its first scheduled tick — neutral grey,
 *    not a failure.
 *  - **ok**: last run succeeded (`r.ok === true`). Green.
 *  - **skipped**: last run failed but the error message starts with
 *    `skipped:` — a known-permanent-state record from a fail-soft
 *    path (e.g. FinMind paywall in PR #183, or a deliberately-off
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
      cls: "bg-green-500/10 text-green-400 border border-green-500/30",
    };
  }
  const isSkipped = (r.error?.toLowerCase().startsWith("skipped") ?? false);
  if (isSkipped) {
    return {
      text: "skipped",
      cls: "bg-amber-500/10 text-amber-400 border border-amber-500/30",
    };
  }
  return {
    text: "error",
    cls: "bg-red-500/10 text-red-400 border border-red-500/30",
  };
}

// Mirror of `RETRYABLE_INGEST_JOBS` in `backend/api/admin/router.py`.
// Adding a job here without registering it backend-side just makes the
// button render — the POST will 404. Keep the two sides in sync.
const RETRYABLE_INGEST_JOBS = new Set([
  "ingest_news_tw",
  "ingest_news_international",
  "ingest_institutional_tw",
  "ingest_margin_tw",
  "ingest_revenue_tw",
  "ingest_revenue_tw_slow",
  "ingest_buyback_tw",
  "ingest_govt_bank_flow_tw",
  "ingest_taiex_history",
  "score_discussion_outcomes",
]);

// Per-job metadata for the IngestHealthCard. `schedule_zh` is mirrored
// from the corresponding `add_job` call in `backend/tasks/scheduler.py`
// — when you change the cron expression there, update the entry here
// or the displayed schedule will silently drift out of sync.
// `description_zh` is a 1-line summary in 繁體中文 so the table is
// readable for non-engineering operators.
interface JobMeta {
  description_zh: string;
  schedule_zh: string;
}
const JOB_META: Record<string, JobMeta> = {
  ingest_news_tw: {
    description_zh: "台股新聞抓取（Google News RSS）",
    schedule_zh: "每 1 小時",
  },
  ingest_news_international: {
    description_zh: "國際財經新聞抓取（Fed / 美股 / 國際）",
    schedule_zh: "每 1 小時",
  },
  ingest_ohlcv_tw: {
    description_zh: "台股每日 K 線（TWSE → FinMind 後備）",
    schedule_zh: "每天 14:30 (台北)",
  },
  ingest_fundamentals_tw: {
    description_zh: "台股基本面（PE / PB / 殖利率）",
    schedule_zh: "每天 14:45 (台北)",
  },
  ingest_institutional_tw: {
    description_zh: "台股法人買賣超（外資 / 投信 / 自營商）",
    schedule_zh: "每天 14:50 (台北)",
  },
  ingest_margin_tw: {
    description_zh: "台股融資融券餘額",
    schedule_zh: "每天 15:00 (台北)",
  },
  ingest_taiex_history: {
    description_zh: "TAIEX 大盤指數每日歷史線",
    schedule_zh: "每天 15:10 (台北)",
  },
  ingest_revenue_tw: {
    description_zh: "台股月營收（FinMind 全市場一次抓，sponsor tier）",
    schedule_zh: "每天 17:00 (台北)",
  },
  ingest_revenue_tw_slow: {
    description_zh: "台股月營收（MOPS per-symbol fallback, 預設關閉；FinMind 出問題時手動重啟）",
    schedule_zh: "停用中",
  },
  ingest_buyback_tw: {
    description_zh: "台股庫藏股公告（FinMind sponsor — 公司自家買回是強訊號）",
    schedule_zh: "每天 18:00 (台北)",
  },
  ingest_govt_bank_flow_tw: {
    description_zh: "台股八大行庫每日買賣金額（FinMind sponsor — 國家隊指標）",
    schedule_zh: "每天 18:30 (台北)",
  },
  ingest_quotes_retention_tw: {
    description_zh: "台股 quote_snapshots 30 日保留（清舊資料）",
    schedule_zh: "每天 11:00 (台北)",
  },
  score_news_sentiment: {
    description_zh: "新聞情緒評分（LLM 評每篇利多 / 利空 / 中性）",
    schedule_zh: "每 30 分鐘",
  },
  auto_run_discussion: {
    description_zh: "每日自動圓桌討論（已啟用 opt-in 的使用者）",
    schedule_zh: "每天 08:00 (台北)",
  },
  verify_discussion_outcome: {
    description_zh: "圓桌討論勝負判定（max-high vs day1_open × 1.03）",
    schedule_zh: "每天 16:30 (台北)",
  },
  score_discussion_outcomes: {
    description_zh: "圓桌討論「對答案」D1-D5 收盤漲跌計算",
    schedule_zh: "每天 17:30 (台北)",
  },
};

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

export function IngestHealthCard() {
  const qc = useQueryClient();
  const { open, toggle } = useCollapsible("admin.ingest-health");
  const { data: serverRows = [], isLoading } = useQuery<IngestHealth[]>({
    queryKey: ["admin", "ingest-health"],
    queryFn: () => api.get("/admin/ingest/health").then((r) => r.data),
    refetchInterval: 60_000,
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

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title="Scheduled Ingest Health"
        headerRight={
          <span className="text-[10px] text-muted-foreground">
            refreshes every 60s
          </span>
        }
      />
      {open && (<>
      {retry.isSuccess && (
        <p className="text-xs text-green-500">{retry.data.message}</p>
      )}
      {retry.isError && (
        <p className="text-xs text-red-500">Retry request failed. Please try again.</p>
      )}

      {isLoading ? (
        <p className="text-xs text-muted-foreground animate-pulse">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No ingest jobs have reported yet — first cron tick is pending.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-[10px] text-muted-foreground uppercase tracking-wider">
                <th className="text-left py-1.5 pr-3">Job</th>
                <th className="text-left py-1.5 pr-3">排程</th>
                <th className="text-left py-1.5 pr-3">Status</th>
                <th className="text-right py-1.5 pr-3">Rows</th>
                <th className="text-left py-1.5 pr-3">Last Run</th>
                <th className="text-left py-1.5">Error</th>
                <th className="text-right py-1.5 pl-3">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {rows.map((r) => {
                const { text: badgeText, cls: badgeCls } = deriveIngestBadge(r);
                const meta = JOB_META[r.job_id];
                return (
                <tr key={r.job_id}>
                  <td className="py-1.5 pr-3 align-top">
                    <div className="font-mono">{r.job_id}</div>
                    {meta && (
                      <div className="text-[10px] text-muted-foreground/80 mt-0.5">
                        {meta.description_zh}
                      </div>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground align-top whitespace-nowrap">
                    {meta?.schedule_zh ?? "—"}
                  </td>
                  <td className="py-1.5 pr-3 align-top">
                    <span
                      className={`px-1.5 py-0.5 rounded text-[10px] ${badgeCls}`}
                    >
                      {badgeText}
                    </span>
                  </td>
                  <td className="py-1.5 pr-3 text-right tabular-nums align-top">
                    {r.row_count.toLocaleString()}
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground align-top">
                    {timeAgo(r.last_run_at)}
                  </td>
                  <td
                    className="py-1.5 text-muted-foreground truncate max-w-[24rem] align-top"
                    title={r.error ?? undefined}
                  >
                    {r.error ?? ""}
                  </td>
                  <td className="py-1.5 pl-3 text-right align-top">
                    {RETRYABLE_INGEST_JOBS.has(r.job_id) ? (
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
                    )}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      </>)}
    </div>
  );
}
