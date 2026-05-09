import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";

/**
 * 7-day outcome sparkline for one ingest job.
 *
 * Each cell = one UTC day. Color resolves with this priority so the
 * eye picks out the worst outcome of the day:
 *   any failed     → red
 *   any silent_deny → purple
 *   any skipped    → amber
 *   only ok        → green
 *   no data        → gray
 *
 * The seven cells render as a fixed-width strip (oldest → newest),
 * left-padded with gray when the history doesn't yet cover the
 * full window (e.g. the cron's first day after deployment). Hovering
 * any cell shows the date + per-outcome counts via the native
 * `title` attribute so operators can drill in without leaving the
 * card.
 */
export interface SparklineDay {
  date: string;
  ok: number;
  silent_deny: number;
  failed: number;
  skipped: number;
}

interface SparklineHistory {
  job_id: string;
  days: number;
  history: SparklineDay[];
}

const WINDOW_DAYS = 7;


/** Build the rolling list of UTC dates the strip should render,
 *  oldest-first. Anchored at "today UTC" so a cell with today's
 *  date sits at the right edge of the strip — operators read the
 *  most recent state where they expect it. */
export function rollingDates(today: Date = new Date(), days: number = WINDOW_DAYS): string[] {
  const dates: string[] = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    dates.push(d.toISOString().slice(0, 10));
  }
  return dates;
}


/** Worst-outcome-wins color rule. Returns Tailwind classes for the
 *  cell + the human-readable label used in the tooltip. Pulled out
 *  of the JSX so it can be unit-tested without rendering. */
export function classifyDay(day: SparklineDay | null): { cls: string; label: string } {
  if (!day) {
    return {
      cls: "bg-muted/20 border border-border/40",
      label: "no data",
    };
  }
  if (day.failed > 0) {
    return {
      cls: "bg-red-500/40 border border-red-500/60",
      label: "failed",
    };
  }
  if (day.silent_deny > 0) {
    return {
      cls: "bg-purple-500/40 border border-purple-500/60",
      label: "silent deny",
    };
  }
  if (day.skipped > 0 && day.ok === 0) {
    return {
      cls: "bg-amber-500/40 border border-amber-500/60",
      label: "skipped",
    };
  }
  if (day.ok > 0) {
    return {
      cls: "bg-green-500/40 border border-green-500/60",
      label: "ok",
    };
  }
  return {
    cls: "bg-muted/20 border border-border/40",
    label: "no data",
  };
}


export function IngestHealthSparkline({ jobId }: { jobId: string }) {
  const { data, isLoading, isError } = useQuery<SparklineHistory>({
    queryKey: ["admin", "ingest", "history", jobId],
    queryFn: async () => {
      const r = await api.get<SparklineHistory>(
        `/admin/ingest/${jobId}/history`,
        { params: { days: WINDOW_DAYS } },
      );
      return r.data;
    },
    staleTime: 60_000,
  });

  if (isLoading || isError || !data) {
    // Render a placeholder strip so the column width stays constant
    // while the query resolves — avoids a layout jump when 24 rows
    // hydrate at slightly different times.
    return (
      <div className="flex gap-0.5" aria-label="loading sparkline">
        {Array.from({ length: WINDOW_DAYS }).map((_, i) => (
          <span
            key={i}
            className="inline-block h-3 w-2 rounded-sm bg-muted/20 border border-border/30"
          />
        ))}
      </div>
    );
  }

  const byDate = new Map(data.history.map((d) => [d.date, d]));
  const dates = rollingDates();

  return (
    <div className="flex gap-0.5">
      {dates.map((date) => {
        const day = byDate.get(date) ?? null;
        const { cls, label } = classifyDay(day);
        const counts = day
          ? `ok=${day.ok}, silent_deny=${day.silent_deny}, failed=${day.failed}, skipped=${day.skipped}`
          : "no runs recorded";
        return (
          <span
            key={date}
            title={`${date} — ${label} (${counts})`}
            className={`inline-block h-3 w-2 rounded-sm ${cls}`}
          />
        );
      })}
    </div>
  );
}
