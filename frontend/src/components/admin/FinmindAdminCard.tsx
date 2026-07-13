import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import api, { errorDetail } from "@/lib/api";

import { ConfigPanel } from "./FinmindAdmin/ConfigPanel";
import { DatasetTable } from "./FinmindAdmin/DatasetTable";
import { RecentErrors } from "./FinmindAdmin/RecentErrors";
import { SetupChecklist } from "./FinmindAdmin/SetupChecklist";
import { StatusBanner } from "./FinmindAdmin/StatusBanner";
import type {
  FinmindConfig,
  FinmindDataset,
  FinmindStatus,
  QuickStartResponse,
  RunDatasetResult,
  RunDueResponse,
  SetupStatusResponse,
} from "./FinmindAdmin/types";

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
 *
 * This entry file owns ALL state/queries/mutations/derived data; the
 * pure display sections live in `./FinmindAdmin/*` (R7/G8 split).
 */

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

  // Resolved env-var settings. Independent of /status because /config
  // doesn't touch the DB — readable even when the FinMind clone is
  // unreachable, which is exactly when the operator most needs to see
  // whether FINMIND_USE_MAIN_DB actually propagated.
  const configQuery = useQuery<FinmindConfig>({
    queryKey: ["admin", "finmind", "config"],
    queryFn: async () => {
      const r = await api.get<FinmindConfig>("/admin/finmind/config");
      return r.data;
    },
    enabled: open,
  });

  const [quickStartResult, setQuickStartResult] =
    useState<QuickStartResponse | null>(null);

  const quickStartMutation = useMutation({
    mutationFn: async () => {
      const r = await api.post<QuickStartResponse>(
        "/admin/finmind/quick-start",
      );
      return r.data;
    },
    onSuccess: (data) => {
      setQuickStartResult(data);
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "datasets"],
      });
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "setup-status"],
      });
      queryClient.invalidateQueries({
        queryKey: ["admin", "finmind", "status"],
      });
    },
  });

  const setupQuery = useQuery<SetupStatusResponse>({
    queryKey: ["admin", "finmind", "setup-status"],
    queryFn: async () => {
      const r = await api.get<SetupStatusResponse>(
        "/admin/finmind/setup-status",
      );
      return r.data;
    },
    enabled: open,
    refetchInterval: 60_000,
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
  const [testConnectionResult, setTestConnectionResult] = useState<{
    ok: boolean;
    message: string;
    token_present: boolean;
    dataset_tested: string;
    rows_returned: number;
  } | null>(null);

  const testConnectionMutation = useMutation({
    mutationFn: async () => {
      const r = await api.post<{
        ok: boolean;
        message: string;
        token_present: boolean;
        dataset_tested: string;
        rows_returned: number;
      }>("/admin/finmind/test-connection");
      return r.data;
    },
    onSuccess: (data) => {
      setTestConnectionResult(data);
    },
  });

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
          {/* Resolved config — mirrors the lifespan startup log so
              the operator can verify env-var propagation directly in
              the UI. Renders regardless of /status outcome. */}
          <ConfigPanel configQuery={configQuery} />

          {/* Status banner ─────────────────────────── */}
          <StatusBanner statusQuery={statusQuery} />

          {/* Setup checklist (first-run wizard) ───── */}
          <SetupChecklist
            setupQuery={setupQuery}
            quickStartMutation={quickStartMutation}
            quickStartResult={quickStartResult}
          />

          {/* Test FinMind connection ────────────── */}
          <div className="rounded border border-border bg-muted/20 p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="text-sm">
                <div className="font-semibold">Test FinMind connection</div>
                <div className="text-xs text-muted-foreground">
                  Pings FinMind with a small free-tier query
                  (TaiwanStockInfo). Use this to verify the
                  FINMIND_TOKEN works + quota isn&apos;t exhausted before
                  enabling datasets.
                </div>
              </div>
              <button
                type="button"
                onClick={() => testConnectionMutation.mutate()}
                disabled={testConnectionMutation.isPending}
                className="rounded border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50"
              >
                {testConnectionMutation.isPending
                  ? "Testing…"
                  : "Test connection"}
              </button>
            </div>
            {testConnectionResult && (
              <div
                className={`mt-2 rounded border p-2 text-xs ${
                  testConnectionResult.ok
                    ? "border-success/40 bg-success/10"
                    : "border-warning/40 bg-warning/10"
                }`}
              >
                <div className="font-semibold">
                  {testConnectionResult.ok ? "✓" : "⚠"}{" "}
                  {testConnectionResult.message}
                </div>
                <div className="mt-0.5 text-muted-foreground">
                  Token present:{" "}
                  {testConnectionResult.token_present ? "yes" : "no"} ·
                  rows returned: {testConnectionResult.rows_returned}
                </div>
              </div>
            )}
            {testConnectionMutation.isError && (
              <div className="mt-2 break-words text-xs text-destructive">
                Test failed: {errorDetail(testConnectionMutation.error)}
              </div>
            )}
          </div>

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
          <DatasetTable
            datasetsQuery={datasetsQuery}
            filtered={filtered}
            updateMutation={updateMutation}
            runDatasetMutation={runDatasetMutation}
            runResults={runResults}
          />

          {/* Recent errors ─────────────────────────── */}
          <RecentErrors statusQuery={statusQuery} />
        </div>
      )}
    </div>
  );
}
