import type { ReactNode } from "react";

// Shared Analytics panel helpers — pulled out of AnalyticsPage (W-final G8)
// so DCF/VaR/Backtest panels can live in their own files. Behaviour identical.

export const inputCls =
  "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
export const labelCls = "block text-xs text-muted-foreground mb-1";

export function Card({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="bg-card shadow-highlight border border-border rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-foreground font-medium">{title}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}

export function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-secondary/30 rounded p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`text-sm font-semibold mt-0.5 ${color ?? "text-foreground"}`}>{value}</p>
    </div>
  );
}
