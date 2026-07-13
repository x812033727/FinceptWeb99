/**
 * Setup checklist (first-run wizard) + Quick Start bulk-enable. Pure
 * display of `setupQuery` / `quickStartMutation` / `quickStartResult`;
 * extracted verbatim from FinmindAdminCard (R7/G8). The original
 * `{setupQuery.data && (…)}` guard becomes an early `null` return so
 * the moved JSX stays byte-identical.
 */
import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";

import { errorDetail } from "@/lib/api";

import type { QuickStartResponse, SetupStatusResponse } from "./types";

export function SetupChecklist({
  setupQuery,
  quickStartMutation,
  quickStartResult,
}: {
  setupQuery: UseQueryResult<SetupStatusResponse>;
  quickStartMutation: UseMutationResult<QuickStartResponse, Error, void>;
  quickStartResult: QuickStartResponse | null;
}) {
  return setupQuery.data ? (
    <div
      className={`rounded border p-3 ${
        setupQuery.data.next_action
          ? "border-warning/40 bg-warning/10"
          : "border-success/40 bg-success/10"
      }`}
    >
      <h3 className="mb-2 text-sm font-semibold">
        {setupQuery.data.next_action
          ? "Setup checklist"
          : "✓ Setup complete"}
      </h3>
      <ul className="space-y-1 text-xs">
        {setupQuery.data.checks.map((c) => (
          <li
            key={c.key}
            className="flex items-start gap-2"
          >
            <span
              className={
                c.passed
                  ? "text-success"
                  : "text-warning"
              }
            >
              {c.passed ? "✓" : "✗"}
            </span>
            <div className="flex-1">
              <div>{c.label}</div>
              {!c.passed && c.detail && (
                <div className="text-muted-foreground">
                  {c.detail}
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
      {setupQuery.data.next_action && (
        <div className="mt-2 rounded border border-border bg-background p-2 text-xs">
          <span className="font-semibold">Next:</span>{" "}
          {setupQuery.data.next_action}
        </div>
      )}

      {/* Quick Start — only surfaced once setup checklist
          passes catalog_seeded (so the bulk-enable can
          actually flip rows). For earlier failure modes
          the operator needs to fix the prerequisite first. */}
      {setupQuery.data.checks.find(
        (c) => c.key === "catalog_seeded",
      )?.passed && (
        <div className="mt-3 flex items-start justify-between gap-3 border-t border-border pt-2">
          <div className="text-xs">
            <div className="font-semibold">Quick Start</div>
            <div className="text-muted-foreground">
              One click bulk-enables a curated set of 11
              recommended datasets (master / price / chip /
              revenue / valuation / dividends). You can
              individually toggle the rest below.
            </div>
          </div>
          <button
            type="button"
            onClick={() => quickStartMutation.mutate()}
            disabled={quickStartMutation.isPending}
            className="shrink-0 rounded bg-primary px-3 py-1.5 text-xs text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          >
            {quickStartMutation.isPending
              ? "Enabling…"
              : "Enable recommended"}
          </button>
        </div>
      )}
      {quickStartResult && (
        <div className="mt-2 rounded border border-border bg-background p-2 text-xs">
          <div>{quickStartResult.note}</div>
          {quickStartResult.skipped.length > 0 && (
            <div className="mt-1 text-muted-foreground">
              Skipped:{" "}
              {quickStartResult.skipped.join(", ")}
            </div>
          )}
        </div>
      )}
      {quickStartMutation.isError && (
        <div className="mt-2 break-words text-xs text-destructive">
          Quick start failed:{" "}
          {errorDetail(quickStartMutation.error)}
        </div>
      )}
    </div>
  ) : null;
}
