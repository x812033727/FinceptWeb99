/**
 * Status banner — alembic head + catalog seeded count + Phase 1
 * schema coverage progress bars per category. One glance tells the
 * operator "is the subsystem alive?". Pure display of `statusQuery`;
 * extracted verbatim from FinmindAdminCard (R7/G8). The trivial
 * `ProgressBar` helper moved here since it's only used by the coverage
 * bars.
 */
import type { UseQueryResult } from "@tanstack/react-query";
import type { AxiosError } from "axios";

import { errorDetail } from "@/lib/api";
import { formatProgressPct } from "@/lib/formatters";

import type { FinmindStatus } from "./types";

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

export function StatusBanner({
  statusQuery,
}: {
  statusQuery: UseQueryResult<FinmindStatus>;
}) {
  return (
    <>
      {statusQuery.isError && (() => {
        const status =
          (statusQuery.error as AxiosError | undefined)?.response?.status;
        // 503 = backend confirmed DB is unreachable. Setup checklist
        // below renders the actionable fix, so we just leave a quiet
        // pointer instead of the alarming red banner.
        if (status === 503) {
          return (
            <div className="rounded border border-warning/40 bg-warning/10 p-3 text-sm">
              <div className="font-semibold">
                FinMind clone DB unreachable
              </div>
              <div className="mt-1 text-xs text-muted-foreground">
                {errorDetail(statusQuery.error)}
              </div>
            </div>
          );
        }
        return (
          <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm">
            Failed to load FinMind status:{" "}
            {errorDetail(statusQuery.error)}
          </div>
        );
      })()}

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
                      ? "text-success"
                      : "text-warning"
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
                      ? "text-success"
                      : "text-warning"
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
                        {c.built}/{c.total} ({formatProgressPct(c.built, c.total)})
                      </span>
                    </div>
                    <ProgressBar built={c.built} total={c.total} />
                  </div>
                ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
