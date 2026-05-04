import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";
import api, { errorDetail } from "@/lib/api";

/**
 * AdminPage card for the FinMind clone subsystem (`backend/finmind/`).
 * Three sub-views, all backed by `/api/admin/finmind/*` (which uses
 * the main app's JWT admin auth, NOT the finmind X-Finmind-Admin-Key
 * — see `api/admin/finmind_proxy.py` for the rationale):
 *
 *   1. Status banner — alembic head + catalog seeded count + Phase 1
 *      schema coverage progress bars per category. One glance tells
 *      the operator "is the subsystem alive?"
 *   2. Catalog table — every dataset_sources row with an
 *      enabled toggle + active_source dropdown. Mutations call
 *      PATCH /api/admin/finmind/datasets/{code} which UPDATEs in
 *      place; query auto-invalidates so the table re-renders.
 *   3. Recent errors — last 24h, merged from dataset_sources +
 *      backfill_progress. Click a row to expand the full message.
 *
 * No usage chart yet — that's coming in the next batch as a
 * dedicated UsageCard so this file stays focused on operator
 * actions.
 */

interface FinmindDataset {
  dataset_code: string;
  category: string;
  description_zh: string;
  local_table: string;
  per_symbol: boolean;
  primary_source: string;
  fallback_source: string | null;
  active_source: string;
  enabled: boolean;
  sponsor_tier: boolean;
  ingest_freq: string;
  last_ingest_at: string | null;
  last_ingest_rows: number | null;
  last_error: string | null;
}

interface RunDatasetResult {
  dataset_code: string;
  symbol: string | null;
  range_start: string;
  range_end: string;
  status: "done" | "failed" | "skipped";
  rows_written: number;
  error: string | null;
}

interface RunDueResponse {
  total: number;
  done: number;
  failed: number;
  skipped: number;
  rows_written: number;
  outcomes: RunDatasetResult[];
}

interface FinmindStatus {
  alembic: {
    at_head: boolean;
    current: string | null;
    expected: string;
  };
  catalog: { seeded: number; expected: number; ok: boolean };
  phase1_coverage: Record<string, { built: number; total: number }>;
  active_ingestion: { enabled: number; by_category: Record<string, number> };
  backfill: Record<string, number>;
  recent_errors: Array<{
    source: string;
    dataset_code: string;
    symbol?: string | null;
    error: string;
    ts: string | null;
  }>;
  generated_at: string;
}

const VALID_SOURCES = ["finmind", "twse", "tpex", "taifex", "mops", "tdcc"];

function formatPct(built: number, total: number): string {
  if (total === 0) return "0%";
  return `${Math.round((built / total) * 100)}%`;
}

function ProgressBar({ built, total }: { built: number; total: number }) {
  const pct = total === 0 ? 0 : (built / total) * 100;
  return (
    <div className="h-2 w-full overflow-hidden rounded bg-muted">
      <div
        className="h-full bg-primary transition-all"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

export default function FinmindAdminCard() {
  const { open, toggle } = useCollapsible("admin-finmind", false);
  const queryClient = useQueryClient();
  const [categoryFilter, setCategoryFilter] = useState<string>("all");
  const [showOnlyEnabled, setShowOnlyEnabled] = useState(false);

  const statusQuery = useQuery<FinmindStatus>({
    queryKey: ["admin", "finmind", "status"],
    queryFn: async () => {
      const r = await api.get<FinmindStatus>("/admin/finmind/status");
      return r.data;
    },
    enabled: open,
    refetchInterval: 30_000,
  });

  const datasetsQuery = useQuery<FinmindDataset[]>({
    queryKey: ["admin", "finmind", "datasets"],
    queryFn: async () => {
      const r = await api.get<FinmindDataset[]>("/admin/finmind/datasets");
      return r.data;
    },
    enabled: open,
  });

  // "Run all due now" button — fires `run_due_now` server-side. The
  // result summary stays in component state until the next click so
  // the operator can read the breakdown.
  const [runDueResult, setRunDueResult] = useState<RunDueResponse | null>(null);
  const runDueMutation = useMutation({
    mutationFn: async () => {
      const r = await api.post<RunDueResponse>("/admin/finmind/run-due");
      return r.data;
    },
    onSuccess: (data) => {
      setRunDueResult(data);
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "status"],
      });
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "datasets"],
      });
    },
  });

  // Per-row "Run" — synchronous single-dataset trigger. Result stored
  // keyed by dataset_code so multiple per-row clicks can show their
  // outcomes side-by-side.
  const [runResults, setRunResults] = useState<
    Record<string, RunDatasetResult>
  >({});
  const runDatasetMutation = useMutation({
    mutationFn: async ({
      dataset_code,
      symbol,
    }: {
      dataset_code: string;
      symbol?: string;
    }) => {
      const r = await api.post<RunDatasetResult>(
        `/admin/finmind/datasets/${dataset_code}/run`,
        { symbol: symbol || null },
      );
      return r.data;
    },
    onSuccess: (data) => {
      setRunResults((prev) => ({ ...prev, [data.dataset_code]: data }));
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "datasets"],
      });
    },
  });

  const updateMutation = useMutation({
    mutationFn: async ({
      dataset_code,
      patch,
    }: {
      dataset_code: string;
      patch: Partial<Pick<FinmindDataset, "enabled" | "active_source">>;
    }) => {
      const r = await api.patch<FinmindDataset>(
        `/admin/finmind/datasets/${dataset_code}`,
        patch,
      );
      return r.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "datasets"],
      });
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "status"],
      });
    },
  });

  const datasets = datasetsQuery.data ?? [];
  const filtered = datasets.filter((d) => {
    if (categoryFilter !== "all" && d.category !== categoryFilter) return false;
    if (showOnlyEnabled && !d.enabled) return false;
    return true;
  });

  const categories = Array.from(new Set(datasets.map((d) => d.category))).sort();

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <CollapsibleHeader
        title="FinMind Clone Subsystem"
        subtitle={
          statusQuery.data
            ? `${statusQuery.data.catalog.seeded}/${statusQuery.data.catalog.expected} datasets · ${statusQuery.data.active_ingestion.enabled} enabled · alembic ${statusQuery.data.alembic.current ?? "n/a"}`
            : "click to expand"
        }
        open={open}
        toggle={toggle}
      />

      {open && (
        <div className="mt-4 space-y-6">
          {/* Status banner ─────────────────────────── */}
          {statusQuery.isError && (
            <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm">
              Failed to load FinMind status:{" "}
              {errorDetail(statusQuery.error)}
              <div className="mt-1 text-xs text-muted-foreground">
                Most likely the FinMind clone DB isn&apos;t reachable. Run{" "}
                <code className="rounded bg-muted px-1">
                  docker compose --profile finmind up -d postgres_finmind
                </code>{" "}
                + <code className="rounded bg-muted px-1">
                  python -m finmind.scripts.init_db
                </code>{" "}
                to bring it up.
              </div>
            </div>
          )}

          {statusQuery.data && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div>
                <h3 className="mb-2 text-sm font-semibold">
                  Migrations + Catalog
                </h3>
                <div className="space-y-1 text-sm">
                  <div className="flex justify-between">
                    <span>Alembic</span>
                    <span
                      className={
                        statusQuery.data.alembic.at_head
                          ? "text-green-600"
                          : "text-amber-600"
                      }
                    >
                      {statusQuery.data.alembic.at_head ? "✓" : "✗"}{" "}
                      {statusQuery.data.alembic.current ?? "uninitialized"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Catalog</span>
                    <span
                      className={
                        statusQuery.data.catalog.ok
                          ? "text-green-600"
                          : "text-amber-600"
                      }
                    >
                      {statusQuery.data.catalog.seeded}/
                      {statusQuery.data.catalog.expected}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Active ingestion</span>
                    <span>
                      {statusQuery.data.active_ingestion.enabled} datasets
                    </span>
                  </div>
                </div>
              </div>

              <div>
                <h3 className="mb-2 text-sm font-semibold">
                  Phase 1 Schema Coverage
                </h3>
                <div className="space-y-2">
                  {Object.entries(statusQuery.data.phase1_coverage)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([cat, c]) => (
                      <div key={cat} className="text-xs">
                        <div className="mb-0.5 flex justify-between">
                          <span>{cat}</span>
                          <span className="text-muted-foreground">
                            {c.built}/{c.total} ({formatPct(c.built, c.total)})
                          </span>
                        </div>
                        <ProgressBar built={c.built} total={c.total} />
                      </div>
                    ))}
                </div>
              </div>
            </div>
          )}

          {/* Run-all-due button + last result ────── */}
          <div className="rounded border border-border bg-muted/20 p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="text-sm">
                <div className="font-semibold">Manual refresh</div>
                <div className="text-xs text-muted-foreground">
                  Triggers `run_due_now` for every enabled dataset
                  whose last_ingest_at is stale. Per-symbol datasets
                  fan across the universe in `tw_stock_info`. Same
                  logic as the cron — useful for forcing a refresh
                  outside the scheduled window.
                </div>
              </div>
              <button
                type="button"
                onClick={() => runDueMutation.mutate()}
                disabled={runDueMutation.isPending}
                className="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              >
                {runDueMutation.isPending ? "Running…" : "Run all due now"}
              </button>
            </div>
            {runDueResult && (
              <div className="mt-2 text-xs">
                <div className="font-mono">
                  {runDueResult.total} chunks — done={runDueResult.done},
                  failed={runDueResult.failed}, skipped=
                  {runDueResult.skipped}, {runDueResult.rows_written} rows
                </div>
                {runDueResult.outcomes
                  .filter((o) => o.status === "failed")
                  .slice(0, 5)
                  .map((o, i) => (
                    <div
                      key={`${o.dataset_code}-${i}`}
                      className="mt-0.5 text-destructive"
                    >
                      ✗ {o.dataset_code}
                      {o.symbol ? ` (${o.symbol})` : ""}: {o.error}
                    </div>
                  ))}
              </div>
            )}
            {runDueMutation.isError && (
              <div className="mt-2 break-words text-xs text-destructive">
                Run failed: {errorDetail(runDueMutation.error)}
              </div>
            )}
          </div>

          {/* Filter row ─────────────────────────── */}
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-2">
              Category
              <select
                value={categoryFilter}
                onChange={(e) => setCategoryFilter(e.target.value)}
                className="rounded border border-border bg-background px-2 py-1"
              >
                <option value="all">all</option>
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={showOnlyEnabled}
                onChange={(e) => setShowOnlyEnabled(e.target.checked)}
              />
              Only enabled
            </label>
            <span className="text-muted-foreground">
              {filtered.length} / {datasets.length} datasets
            </span>
          </div>

          {/* Dataset table ─────────────────────────── */}
          {datasetsQuery.isLoading && (
            <div className="text-sm text-muted-foreground">Loading…</div>
          )}
          {datasetsQuery.data && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="border-b border-border text-left">
                  <tr>
                    <th className="py-2 pr-2">Dataset</th>
                    <th className="py-2 pr-2">Category</th>
                    <th className="py-2 pr-2">Local table</th>
                    <th className="py-2 pr-2">Source</th>
                    <th className="py-2 pr-2">Last ingest</th>
                    <th className="py-2 pr-2">Enabled</th>
                    <th className="py-2 pr-2">Run</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((d) => (
                    <tr
                      key={d.dataset_code}
                      className="border-b border-border last:border-b-0 hover:bg-muted/30"
                      title={d.description_zh}
                    >
                      <td className="py-1.5 pr-2 font-mono">
                        {d.dataset_code}
                        {d.sponsor_tier && (
                          <span
                            className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-800"
                            title="FinMind sponsor-tier dataset"
                          >
                            sponsor
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-2 text-muted-foreground">
                        {d.category}
                      </td>
                      <td className="py-1.5 pr-2 font-mono text-muted-foreground">
                        {d.local_table || (
                          <span className="italic text-amber-600">
                            (not built)
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-2">
                        <select
                          value={d.active_source}
                          onChange={(e) =>
                            updateMutation.mutate({
                              dataset_code: d.dataset_code,
                              patch: { active_source: e.target.value },
                            })
                          }
                          disabled={updateMutation.isPending}
                          className="rounded border border-border bg-background px-1 py-0.5"
                        >
                          {VALID_SOURCES.map((s) => (
                            <option key={s} value={s}>
                              {s}
                            </option>
                          ))}
                        </select>
                      </td>
                      <td className="py-1.5 pr-2 text-muted-foreground">
                        {d.last_ingest_at
                          ? new Date(d.last_ingest_at).toLocaleString()
                          : "—"}
                        {d.last_error && (
                          <span
                            className="ml-1 text-destructive"
                            title={d.last_error}
                          >
                            ⚠
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 pr-2">
                        <input
                          type="checkbox"
                          checked={d.enabled}
                          onChange={(e) =>
                            updateMutation.mutate({
                              dataset_code: d.dataset_code,
                              patch: { enabled: e.target.checked },
                            })
                          }
                          disabled={
                            !d.local_table || updateMutation.isPending
                          }
                          title={
                            !d.local_table
                              ? "Destination table not built — Phase 1 hasn't migrated this dataset yet"
                              : ""
                          }
                        />
                      </td>
                      <td className="py-1.5 pr-2">
                        <button
                          type="button"
                          onClick={() =>
                            runDatasetMutation.mutate({
                              dataset_code: d.dataset_code,
                            })
                          }
                          disabled={
                            !d.local_table || runDatasetMutation.isPending
                          }
                          className="rounded border border-border px-1.5 py-0.5 text-[10px] hover:bg-muted disabled:opacity-50"
                          title={
                            d.per_symbol
                              ? "Per-symbol dataset — runs without symbol; most upstream sources fail in that mode (use the cron with --universe-from-tw-stock-info for the per-symbol fan-out)"
                              : "Trigger one ingest_chunk for the last 7 days"
                          }
                        >
                          Run
                        </button>
                        {runResults[d.dataset_code] && (
                          <span
                            className={`ml-1 text-[10px] ${
                              runResults[d.dataset_code].status === "done"
                                ? "text-green-600"
                                : runResults[d.dataset_code].status === "skipped"
                                  ? "text-amber-600"
                                  : "text-destructive"
                            }`}
                            title={
                              runResults[d.dataset_code].error || ""
                            }
                          >
                            {runResults[d.dataset_code].status === "done"
                              ? `✓ ${runResults[d.dataset_code].rows_written}`
                              : runResults[d.dataset_code].status === "skipped"
                                ? "○ skip"
                                : "✗"}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Recent errors ─────────────────────────── */}
          {statusQuery.data && statusQuery.data.recent_errors.length > 0 && (
            <div>
              <h3 className="mb-2 text-sm font-semibold">
                Recent errors (last 24h)
              </h3>
              <ul className="space-y-1 text-xs">
                {statusQuery.data.recent_errors.map((e, i) => (
                  <li
                    key={`${e.dataset_code}-${i}`}
                    className="rounded border border-border bg-muted/30 p-2"
                  >
                    <div className="flex justify-between">
                      <span className="font-mono">
                        [{e.source}] {e.dataset_code}
                        {e.symbol ? ` ${e.symbol}` : ""}
                      </span>
                      <span className="text-muted-foreground">
                        {e.ts ? new Date(e.ts).toLocaleString() : "—"}
                      </span>
                    </div>
                    <div className="mt-1 break-all text-destructive">
                      {e.error}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
