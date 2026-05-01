import { describe, expect, it } from "vitest";
import { deriveIngestBadge } from "./IngestHealthCard";

const baseRow = {
  job_id: "ingest_x",
  last_run_at: "2026-05-01T12:00:00Z",
  ok: true,
  row_count: 100,
  error: null,
};

describe("deriveIngestBadge", () => {
  it("returns pending when last_run_at is null", () => {
    const out = deriveIngestBadge({ ...baseRow, last_run_at: null, ok: false });
    expect(out.text).toBe("pending");
    expect(out.cls).toContain("muted-foreground");
  });

  it("returns ok when last run succeeded", () => {
    const out = deriveIngestBadge(baseRow);
    expect(out.text).toBe("ok");
    expect(out.cls).toContain("text-green-400");
  });

  it("returns skipped when error starts with 'skipped:' (FinMind paywall fail-soft case)", () => {
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "skipped: FinMind paywalled this dataset (TaiwanStockMonthRevenue ...)",
    });
    expect(out.text).toBe("skipped");
    expect(out.cls).toContain("text-amber-400");
  });

  it("treats 'Skipped' (case-insensitive) as skipped", () => {
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "Skipped: lock held",
    });
    expect(out.text).toBe("skipped");
  });

  it("matches the cron-armed-backoff skip wording too", () => {
    // `record_health` in `services/ingest/repository` writes
    // "skipped (backoff after N failures, ~M min remaining)" when
    // the auto-backoff window is still active. Same UX category as
    // the FinMind paywall — operator should read it as "deliberately
    // not running right now", not "broken".
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "skipped (backoff after 2 failures, ~60 min remaining)",
    });
    expect(out.text).toBe("skipped");
  });

  it("returns queued (blue) when the admin retry endpoint just kicked off a run", () => {
    // `POST /admin/ingest/{job}/retry` writes a placeholder health
    // record before the background task starts. Without this branch
    // the badge would render red until the real run completes —
    // misleading for slow crons (e.g. ingest_revenue_tw_slow's
    // ~6-minute tick).
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "queued: manual retry — previous backoff cleared",
    });
    expect(out.text).toBe("queued");
    expect(out.cls).toContain("text-blue-400");
  });

  it("returns error (red) for transient failures with auto-backoff armed", () => {
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "HTTP 503 Service Unavailable (failure #1; auto-backoff armed)",
    });
    expect(out.text).toBe("error");
    expect(out.cls).toContain("text-red-400");
  });

  it("returns error when error is null but ok is false (defensive)", () => {
    // Row shape that shouldn't happen in practice but the helper
    // shouldn't crash on it — falls through to the default error
    // bucket, which is the safest signal for "something's off".
    const out = deriveIngestBadge({ ...baseRow, ok: false, error: null });
    expect(out.text).toBe("error");
  });

  it("does NOT match an error message that mentions 'skipped' mid-string", () => {
    // Only the leading word counts — otherwise an error like
    // "HTTP 500: prior tick was skipped, this one failed" would
    // get a false-positive yellow badge.
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "HTTP 500 — prior tick was skipped, this one failed too",
    });
    expect(out.text).toBe("error");
  });

  it("priority: pending beats every other signal when last_run_at is null", () => {
    // Even an `ok=false, error="skipped:..."` row is pending if it
    // has never actually run. `pending` is the most informative
    // signal in that case.
    const out = deriveIngestBadge({
      ...baseRow,
      last_run_at: null,
      ok: false,
      error: "skipped: paywalled",
    });
    expect(out.text).toBe("pending");
  });
});
