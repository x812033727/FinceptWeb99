/**
 * Recent errors — last 24h, merged from dataset_sources +
 * backfill_progress. Pure display of `statusQuery`; extracted verbatim
 * from FinmindAdminCard (R7/G8). The original
 * `{statusQuery.data && statusQuery.data.recent_errors.length > 0 && (…)}`
 * guard becomes a ternary that returns null so the moved JSX stays
 * byte-identical.
 */
import type { UseQueryResult } from "@tanstack/react-query";

import { formatTaipei } from "@/lib/timeFormat";

import type { FinmindStatus } from "./types";

export function RecentErrors({
  statusQuery,
}: {
  statusQuery: UseQueryResult<FinmindStatus>;
}) {
  return statusQuery.data && statusQuery.data.recent_errors.length > 0 ? (
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
                {e.ts ? formatTaipei(e.ts) : "—"}
              </span>
            </div>
            <div className="mt-1 break-all text-destructive">
              {e.error}
            </div>
          </li>
        ))}
      </ul>
    </div>
  ) : null;
}
