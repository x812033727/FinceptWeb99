/**
 * Catalog table — every dataset_sources row with an enabled toggle +
 * active_source dropdown + per-row Run trigger. Pure display of
 * `datasetsQuery` / `filtered` / `updateMutation` / `runDatasetMutation`
 * / `runResults`; extracted verbatim from FinmindAdminCard (R7/G8).
 * `VALID_SOURCES` moved here since the source dropdown is its only use.
 */
import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";
import type { AxiosError } from "axios";

import { errorDetail } from "@/lib/api";
import { formatTaipei } from "@/lib/timeFormat";

import type { FinmindDataset, RunDatasetResult } from "./types";

const VALID_SOURCES = ["finmind", "twse", "tpex", "taifex", "mops", "tdcc"];

export function DatasetTable({
  datasetsQuery,
  filtered,
  updateMutation,
  runDatasetMutation,
  runResults,
}: {
  datasetsQuery: UseQueryResult<FinmindDataset[]>;
  filtered: FinmindDataset[];
  updateMutation: UseMutationResult<
    FinmindDataset,
    Error,
    {
      dataset_code: string;
      patch: Partial<Pick<FinmindDataset, "enabled" | "active_source">>;
    }
  >;
  runDatasetMutation: UseMutationResult<
    RunDatasetResult,
    Error,
    { dataset_code: string; symbol?: string }
  >;
  runResults: Record<string, RunDatasetResult>;
}) {
  return (
    <>
      {datasetsQuery.isLoading && (
        <div className="text-sm text-muted-foreground">Loading…</div>
      )}
      {datasetsQuery.isError && (() => {
        const dsStatus =
          (datasetsQuery.error as AxiosError | undefined)?.response?.status;
        // 503 is already explained by the status banner above —
        // keep this placeholder small so the page doesn't repeat
        // the same diagnosis twice.
        if (dsStatus === 503) {
          return (
            <div className="rounded border border-border bg-muted/20 p-3 text-xs text-muted-foreground">
              Dataset list unavailable while DB is unreachable.
            </div>
          );
        }
        return (
          <div className="rounded border border-destructive bg-destructive/10 p-3 text-xs">
            Failed to load datasets: {errorDetail(datasetsQuery.error)}
          </div>
        );
      })()}
      {datasetsQuery.data && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            {/* Sticky header so column labels stay visible while
                scrolling the 80-row dataset list inside the card. */}
            <thead className="sticky top-0 z-10 border-b border-border bg-card text-left">
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
                        className="ml-1 rounded bg-warning/15 px-1 text-[10px] text-warning"
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
                      <span className="italic text-warning">
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
                      ? formatTaipei(d.last_ingest_at)
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
                            ? "text-success"
                            : runResults[d.dataset_code].status === "skipped"
                              ? "text-warning"
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
    </>
  );
}
