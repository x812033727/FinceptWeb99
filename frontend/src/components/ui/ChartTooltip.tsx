import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Shared Recharts tooltip — the one place chart tooltip chrome is styled,
 * so the R6 palette refinement finally reaches tooltips (the token recolor
 * flows to series/candles automatically, but each Recharts `<Tooltip>` was
 * previously hand-styled via `contentStyle`). Drop-in:
 *
 *   <Tooltip content={<ChartTooltip />} />
 *   <Tooltip content={<ChartTooltip valueFormatter={(v) => `${v}M`} />} />
 *
 * Recharts injects `active` / `payload` / `label` at render time. Values
 * render in `font-mono tabular-nums` so figures read machined.
 */
interface TooltipEntry {
  name?: React.ReactNode;
  value?: number | string;
  color?: string;
  dataKey?: string | number;
}

export interface ChartTooltipProps {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: React.ReactNode;
  /** Format numeric values (name is the series name). */
  valueFormatter?: (value: number, name: string) => React.ReactNode;
  /** Format the tooltip label (x-axis category / date). */
  labelFormatter?: (label: React.ReactNode) => React.ReactNode;
  className?: string;
}

export function ChartTooltip({
  active,
  payload,
  label,
  valueFormatter,
  labelFormatter,
  className,
}: ChartTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  return (
    <div
      className={cn(
        "rounded-md border border-border-strong bg-surface-2 px-3 py-2 text-data shadow-popover",
        className
      )}
    >
      {label !== undefined && label !== null && label !== "" && (
        <div className="mb-1 text-micro text-muted-foreground">
          {labelFormatter ? labelFormatter(label) : String(label)}
        </div>
      )}
      <ul className="space-y-0.5">
        {payload.map((entry, i) => (
          <li key={`${entry.dataKey ?? entry.name ?? i}-${i}`} className="flex items-center gap-2">
            {entry.color && (
              <span
                aria-hidden="true"
                className="h-2 w-2 shrink-0 rounded-[2px]"
                style={{ background: entry.color }}
              />
            )}
            {entry.name !== undefined && (
              <span className="text-muted-foreground">{entry.name}</span>
            )}
            <span className="ml-auto font-mono tabular-nums">
              {valueFormatter && typeof entry.value === "number"
                ? valueFormatter(entry.value, String(entry.name ?? ""))
                : typeof entry.value === "number"
                  ? entry.value.toLocaleString()
                  : entry.value}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * Token-driven Recharts axis tick style — pair with ChartTooltip so axes
 * and tooltips share the same type/color vocabulary. Spread onto XAxis /
 * YAxis `tick`:  <XAxis tick={chartAxisTick} />
 */
export const chartAxisTick = {
  fontSize: 10,
  fill: "hsl(var(--muted-foreground))",
} as const;
