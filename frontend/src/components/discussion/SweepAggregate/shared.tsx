/**
 * Shared presentational primitive for the SweepAggregate sections.
 *
 * `Tile` is the small labelled metric cell used by both the KPI grid
 * (`kpis.tsx`) and the Brier calibration grid (`brier.tsx`), so it
 * lives here rather than in either section to avoid a cross-section
 * import.
 */

/** Small labelled metric tile (label + mono value, optional accent). */
export function Tile({
  label, value, accent, labelTip,
}: {
  label: string;
  value: string;
  accent?: "emerald" | "red";
  labelTip?: string;
}) {
  const cls = accent === "emerald"
    ? "text-success"
    : accent === "red"
    ? "text-danger"
    : "text-foreground";
  return (
    <div className="bg-secondary/30 border border-border rounded p-1.5">
      <p
        className="text-[10px] text-muted-foreground uppercase tracking-wider"
        title={labelTip}
      >
        {label}
      </p>
      <p className={`text-sm font-semibold font-mono ${cls}`}>{value}</p>
    </div>
  );
}
