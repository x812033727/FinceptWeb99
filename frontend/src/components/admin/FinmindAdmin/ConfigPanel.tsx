/**
 * Resolved config panel — mirrors the lifespan startup log so the
 * operator can verify env-var propagation directly in the UI. Renders
 * regardless of /status outcome. Pure display of `configQuery`;
 * extracted verbatim from FinmindAdminCard (R7/G8).
 */
import type { UseQueryResult } from "@tanstack/react-query";

import { errorDetail } from "@/lib/api";

import type { FinmindConfig } from "./types";

export function ConfigPanel({
  configQuery,
}: {
  configQuery: UseQueryResult<FinmindConfig>;
}) {
  return (
    <>
      {configQuery.isError && (
        <div className="rounded border border-warning/40 bg-warning/10 p-3 text-xs">
          <div className="font-semibold">
            Resolved config unavailable
          </div>
          <div className="mt-1 text-muted-foreground">
            {errorDetail(configQuery.error)} — check whether the
            /admin/finmind/config endpoint is wired up.
          </div>
        </div>
      )}
      {configQuery.data && (() => {
        const c = configQuery.data;
        const modeLabel = {
          "separate-container": "Path A1 · separate postgres_finmind container",
          "shared-main-db": "Path A2 · shared main DB via `finmind` schema",
          "sqlite-test": "SQLite (test environment)",
        }[c.mode];
        const modeColor = c.mode === "shared-main-db"
          ? "text-primary"
          : "text-muted-foreground";
        return (
          <div className="rounded border border-border bg-muted/30 p-3 text-xs">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-semibold">Resolved config</span>
              <span className={`font-mono ${modeColor}`}>{modeLabel}</span>
            </div>
            <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
              <div className="flex justify-between gap-2">
                <span className="text-muted-foreground">FINMIND_USE_MAIN_DB</span>
                <span className="font-mono">{String(c.use_main_db)}</span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-muted-foreground">FINMIND_AUTO_INIT</span>
                <span className="font-mono">{String(c.auto_init)}</span>
              </div>
              <div className="flex justify-between gap-2 sm:col-span-2">
                <span className="text-muted-foreground">effective URL</span>
                <span className="font-mono break-all text-right">
                  {c.effective_database_url}
                </span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-muted-foreground">schema</span>
                <span className="font-mono">{c.schema_ ?? "(default)"}</span>
              </div>
            </div>
          </div>
        );
      })()}
    </>
  );
}
