/**
 * C3 — shared types + pure helpers for the backtest compare view.
 * Kept out of BacktestHistoryPanel.tsx so the component file only
 * exports components (react-refresh) and the pivot stays unit-testable.
 */
export interface BacktestRunSummary {
  id: string;
  name: string | null;
  strategy: string;
  created_at: string;
  config: Record<string, unknown>;
  metrics: Record<string, number | undefined>;
}

export interface RunListResponse {
  items: BacktestRunSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface CompareRun {
  id: string;
  name: string | null;
  strategy: string;
  created_at: string;
  metrics: Record<string, number | undefined>;
  values: (number | null)[];
}

export interface CompareResponse {
  dates: string[];
  runs: CompareRun[];
}

export const MAX_COMPARE = 4;

/** Pivot the compare payload into recharts rows: {date, r0, r1, …}. */
export function toChartRows(data: CompareResponse): Record<string, string | number | null>[] {
  return data.dates.map((date, i) => {
    const row: Record<string, string | number | null> = { date };
    data.runs.forEach((run, ri) => { row[`r${ri}`] = run.values[i]; });
    return row;
  });
}
