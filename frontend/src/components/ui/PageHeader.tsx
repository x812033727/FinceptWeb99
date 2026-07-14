import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Standard page heading row: title + optional description on the
 * left, action buttons on the right; stacks vertically below `sm`.
 * Every page adopts this in the R7 per-page relayout so heading
 * hierarchy and spacing stop drifting page-by-page.
 */
export interface PageHeaderProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Right-aligned actions (buttons / selects). */
  actions?: React.ReactNode;
  /** Breadcrumb slot above the title (StockDetail / Market drill-downs). */
  breadcrumb?: React.ReactNode;
  /** id on the <h1> so a page can point aria-labelledby at it. */
  id?: string;
  className?: string;
}

export function PageHeader({ title, description, actions, breadcrumb, id, className }: PageHeaderProps) {
  return (
    <div
      className={cn(
        "flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between",
        className
      )}
    >
      <div className="min-w-0">
        {breadcrumb && <div className="mb-1 text-micro text-muted-foreground">{breadcrumb}</div>}
        <h1 id={id} className="truncate text-title font-semibold">{title}</h1>
        {description && <p className="mt-0.5 text-data text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
