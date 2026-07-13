/**
 * Shared types + constants for the FinmindKeys sections.
 *
 * The three record interfaces (`ApiKeyItem`, `IssuedKeyResponse`,
 * `PlanItem`) are moved verbatim from the old single-file
 * `FinmindKeysCard`; they are consumed both by the entry card (which
 * owns the queries/mutations/state) and by the presentational
 * sections in this directory, so they live here to avoid a
 * cross-section import.
 *
 * `EMAIL_RE` lives here too because it is referenced both by the
 * entry card (submit guard + disabled logic) and by the
 * `IssueKeyForm` section's inline validation hint.
 */

// Loose email regex — same shape as HTML5 input[type=email] uses
// for client-side validation. The backend re-validates anyway, so
// this is purely UX (disable submit + show inline hint).
export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export interface ApiKeyItem {
  id: number;
  prefix: string;
  owner_email: string;
  name: string | null;
  enabled: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
  plan_code: string | null;
  subscription_id: number | null;
}

export interface IssuedKeyResponse {
  record_id: number;
  plaintext: string;
  prefix: string;
  owner_email: string;
  plan_code: string | null;
  subscription_id: number | null;
}

export interface PlanItem {
  code: string;
  name: string;
  price_monthly: number | null;
  price_yearly: number | null;
  currency: string;
  allowed_datasets: string[] | null;
  quota_daily_calls: number;
  quota_daily_rows: number;
  enabled: boolean;
}

/** Local state shape of the inline plan create/edit mini-form. */
export interface PlanFormState {
  code: string;
  name: string;
  price_monthly: string;
  quota_daily_calls: string;
  quota_daily_rows: string;
}

/** Input variables for the issue-key mutation. */
export interface IssueKeyInput {
  owner_email: string;
  name?: string;
  plan_code?: string;
}

/** Input variables for the upsert-plan mutation. */
export interface PlanUpsertInput {
  code: string;
  name: string;
  price_monthly: number | null;
  quota_daily_calls: number;
  quota_daily_rows: number;
}
