import * as React from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Compact KPI tile: label + headline value + optional delta / hint.
 * The value slot is ReactNode so callers compose `<Num>` /
 * `<DeltaText>` for numeric content (keeps sign-coloring in the
 * market-color convention layer, never here).
 */
export interface StatCardProps {
  label: React.ReactNode;
  value: React.ReactNode;
  /** Small trailing node next to the value — typically <DeltaText>. */
  delta?: React.ReactNode;
  icon?: LucideIcon;
  /** Muted footnote under the value (e.g. "as of 14:30"). */
  hint?: React.ReactNode;
  /** Dense variant for stat grids inside cards (smaller value + padding). */
  compact?: boolean;
  className?: string;
}

export function StatCard({ label, value, delta, icon: Icon, hint, compact = false, className }: StatCardProps) {
  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-surface-1",
        compact ? "p-2" : "p-3 sm:p-4",
        className
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <p className={cn("truncate text-muted-foreground", compact ? "text-[10px] uppercase tracking-wider" : "text-xs")}>
          {label}
        </p>
        {Icon && <Icon className="h-4 w-4 shrink-0 text-muted-foreground/60" aria-hidden="true" />}
      </div>
      <div className={cn("flex items-baseline gap-2", compact ? "mt-0.5" : "mt-1")}>
        <span className={cn("font-semibold tracking-tight tabular-nums", compact ? "text-sm" : "text-xl")}>
          {value}
        </span>
        {delta && <span className="text-xs">{delta}</span>}
      </div>
      {hint && <p className="mt-0.5 text-[11px] text-muted-foreground/70">{hint}</p>}
    </div>
  );
}
