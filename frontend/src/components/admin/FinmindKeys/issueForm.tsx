import type { Dispatch, SetStateAction } from "react";
import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";

import {
  EMAIL_RE,
  type IssueKeyInput,
  type IssuedKeyResponse,
  type PlanItem,
} from "./types";

/**
 * The "Issue new key" form. Pure display: owner-email / name / plan
 * inputs bound to the entry card's state, submitting via the entry
 * card's issue mutation. `EMAIL_RE` powers the inline invalid-format
 * hint (the entry card re-uses it for the submit/disabled guard).
 */
export function IssueKeyForm({
  ownerEmail,
  setOwnerEmail,
  keyName,
  setKeyName,
  keyPlanCode,
  setKeyPlanCode,
  issueMutation,
  plansQuery,
}: {
  ownerEmail: string;
  setOwnerEmail: Dispatch<SetStateAction<string>>;
  keyName: string;
  setKeyName: Dispatch<SetStateAction<string>>;
  keyPlanCode: string;
  setKeyPlanCode: Dispatch<SetStateAction<string>>;
  issueMutation: UseMutationResult<IssuedKeyResponse, Error, IssueKeyInput>;
  plansQuery: UseQueryResult<PlanItem[], Error>;
}) {
  return (
    <form
      className="flex flex-wrap items-end gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        const email = ownerEmail.trim();
        if (!email || !EMAIL_RE.test(email)) return;
        issueMutation.mutate({
          owner_email: email,
          name: keyName.trim() || undefined,
          plan_code: keyPlanCode.trim() || undefined,
        });
      }}
    >
      <label className="flex flex-col">
        <span className="text-xs text-muted-foreground">
          Owner email
        </span>
        <input
          type="email"
          required
          value={ownerEmail}
          onChange={(e) => setOwnerEmail(e.target.value)}
          className="rounded border border-border bg-background px-2 py-1"
          placeholder="customer@example.com"
        />
        {ownerEmail.trim().length > 0 &&
          !EMAIL_RE.test(ownerEmail.trim()) && (
            <span className="mt-0.5 text-micro text-destructive">
              Invalid email format
            </span>
          )}
      </label>
      <label className="flex flex-col">
        <span className="text-xs text-muted-foreground">
          Name (optional)
        </span>
        <input
          type="text"
          value={keyName}
          onChange={(e) => setKeyName(e.target.value)}
          className="rounded border border-border bg-background px-2 py-1"
          placeholder="e.g. prod-backtest"
        />
      </label>
      <label className="flex flex-col">
        <span className="text-xs text-muted-foreground">Plan</span>
        <select
          value={keyPlanCode}
          onChange={(e) => setKeyPlanCode(e.target.value)}
          className="rounded border border-border bg-background px-2 py-1"
        >
          <option value="">Free tier (default)</option>
          {(plansQuery.data ?? [])
            .filter((p) => p.enabled)
            .map((p) => (
              <option key={p.code} value={p.code}>
                {p.code} — {p.name}
              </option>
            ))}
        </select>
      </label>
      <button
        type="submit"
        disabled={
          issueMutation.isPending ||
          !ownerEmail.trim() ||
          !EMAIL_RE.test(ownerEmail.trim())
        }
        className="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {issueMutation.isPending ? "Issuing…" : "Issue key"}
      </button>
    </form>
  );
}
