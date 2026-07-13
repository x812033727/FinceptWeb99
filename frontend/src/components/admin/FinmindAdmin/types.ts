/**
 * Shared types for the FinMind admin card and its presentational
 * sub-components (`./FinmindAdmin/*`). Moved verbatim out of
 * `FinmindAdminCard.tsx` during the R7/G8 presentational split so the
 * parent and each child agree on the same shapes. No shape changes.
 */

export interface FinmindDataset {
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

export interface RunDatasetResult {
  dataset_code: string;
  symbol: string | null;
  range_start: string;
  range_end: string;
  status: "done" | "failed" | "skipped";
  rows_written: number;
  error: string | null;
}

export interface RunDueResponse {
  total: number;
  done: number;
  failed: number;
  skipped: number;
  rows_written: number;
  outcomes: RunDatasetResult[];
}

export interface FinmindStatus {
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

export interface FinmindConfig {
  use_main_db: boolean;
  auto_init: boolean;
  effective_database_url: string;
  schema_: string | null;
  mode: "separate-container" | "shared-main-db" | "sqlite-test";
}

export interface QuickStartResponse {
  enabled_count: number;
  skipped: string[];
  enabled: string[];
  note: string;
}

export interface SetupCheck {
  key: string;
  label: string;
  passed: boolean;
  detail: string;
  fix_hint: string;
}

export interface SetupStatusResponse {
  checks: SetupCheck[];
  next_action: string | null;
}
