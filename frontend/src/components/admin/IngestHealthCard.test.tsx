import { describe, expect, it } from "vitest";
import {
  deriveIngestBadge,
  deriveSchedulerBadge,
  isDataStale,
} from "./IngestHealthCard";

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

  it("returns silent deny when error starts with 'silent_paywall:'", () => {
    // FinMind silent-deny: HTTP 200 + body.status != 200. The task
    // layer's `raise_if_silent_denied` translates the contextvar into
    // FinMindSilentDeny which becomes `error="silent_paywall: ..."`.
    // Distinct purple badge so operators can tell tier-issue apart
    // from a real outage at a glance.
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: "silent_paywall: Your level is register, not sponsor.",
    });
    expect(out.text).toBe("silent deny");
    expect(out.cls).toContain("text-purple-400");
  });

  it("returns silent deny when the structured silent_deny field is set", () => {
    // The silent_deny field is the canonical signal. A row with the
    // field populated but no `error` prefix (older serializer) should
    // still get the purple badge.
    const out = deriveIngestBadge({
      ...baseRow,
      ok: false,
      error: null,
      silent_deny: "Your level is register",
    });
    expect(out.text).toBe("silent deny");
  });
});


describe("deriveSchedulerBadge", () => {
  it("returns loading when the query hasn't resolved yet", () => {
    const out = deriveSchedulerBadge(undefined);
    expect(out.text).toBe("loading");
    expect(out.cls).toContain("muted-foreground");
  });

  it("returns scheduler dead (red) when heartbeat key is missing", () => {
    const out = deriveSchedulerBadge({
      last_beat_at: null,
      age_seconds: null,
      stale: true,
      version: null,
      ttl_seconds: 180,
    });
    expect(out.text).toBe("scheduler dead");
    expect(out.cls).toContain("text-red-400");
    expect(out.tooltip).toContain("No heartbeat in Redis");
  });

  it("returns scheduler stale (amber) when last beat older than 60s", () => {
    const out = deriveSchedulerBadge({
      last_beat_at: "2026-05-09T12:00:00+00:00",
      age_seconds: 75,
      stale: true,
      version: "0.5.84",
      ttl_seconds: 180,
    });
    expect(out.text).toContain("75");
    expect(out.text).toContain("scheduler stale");
    expect(out.cls).toContain("text-amber-400");
    expect(out.tooltip).toContain("Event loop may be wedged");
  });

  it("returns scheduler ok (green) when heartbeat is fresh", () => {
    const out = deriveSchedulerBadge({
      last_beat_at: "2026-05-09T12:00:00+00:00",
      age_seconds: 12,
      stale: false,
      version: "0.5.84",
      ttl_seconds: 180,
    });
    expect(out.text).toContain("12");
    expect(out.text).toContain("scheduler ok");
    expect(out.cls).toContain("text-green-400");
  });

  it("rounds the age to the nearest second", () => {
    const out = deriveSchedulerBadge({
      last_beat_at: "2026-05-09T12:00:00+00:00",
      age_seconds: 3.7,
      stale: false,
      version: "0.5.84",
      ttl_seconds: 180,
    });
    expect(out.text).toContain("4");
  });
});


describe("isDataStale", () => {
  const fresh: ReturnType<typeof Object.assign> = {
    job_id: "ingest_x",
    ok: true,
    row_count: 100,
    error: null,
  };

  it("returns false when latest_data_ts is missing (non-time-series job)", () => {
    expect(
      isDataStale({
        ...fresh,
        last_run_at: "2026-05-01T12:00:00Z",
        latest_data_ts: null,
      }),
    ).toBe(false);
  });

  it("returns false when latest_data_ts is within 2 days of last_run_at", () => {
    expect(
      isDataStale({
        ...fresh,
        last_run_at: "2026-05-01T12:00:00Z",
        latest_data_ts: "2026-04-30",
      }),
    ).toBe(false);
  });

  it("returns true when latest_data_ts is more than 2 days behind last_run_at", () => {
    expect(
      isDataStale({
        ...fresh,
        last_run_at: "2026-05-09T12:00:00Z",
        latest_data_ts: "2026-04-30",
      }),
    ).toBe(true);
  });

  it("returns false when both timestamps are missing (initial pending state)", () => {
    expect(
      isDataStale({
        ...fresh,
        last_run_at: null,
        latest_data_ts: null,
      }),
    ).toBe(false);
  });
});
