import * as React from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Shared loading placeholder — the twin of `EmptyState`, for the ~34
 * ad-hoc "載入中…" spinners scattered across pages. Announces politely
 * to assistive tech (role="status" + aria-live) and carries a visually
 * hidden label so screen-reader users hear the busy state even when the
 * visible label is omitted. Content is plain ReactNode (callers pass an
 * already-translated `t("…")` label), keeping the primitive i18n-agnostic.
 */
export interface LoadingStateProps {
  /** Visible + announced label. Falls back to a screen-reader-only
   * "Loading" so the region is never silent. */
  label?: React.ReactNode;
  /** Hide the label visually but keep it for screen readers. */
  labelHidden?: boolean;
  className?: string;
}

export function LoadingState({ label, labelHidden = false, className }: LoadingStateProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex flex-col items-center justify-center gap-2 px-6 py-10 text-center",
        className
      )}
    >
      <Loader2 className="h-6 w-6 animate-spin text-muted-foreground/60" aria-hidden="true" />
      <span className={cn("text-data text-muted-foreground", labelHidden && "sr-only")}>
        {label ?? "Loading…"}
      </span>
    </div>
  );
}
