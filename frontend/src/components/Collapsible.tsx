import { ReactNode } from "react";

interface CollapsibleHeaderProps {
  open: boolean;
  toggle: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  /**
   * Right-aligned content that stays visible regardless of collapse
   * state (e.g. status badges, action buttons). Click events here
   * don't toggle the card — the chevron / title area is the toggle
   * surface.
   */
  headerRight?: ReactNode;
}

export function CollapsibleHeader({
  open, toggle, title, subtitle, headerRight,
}: CollapsibleHeaderProps) {
  return (
    <div className="flex items-start gap-2">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex-1 flex items-start gap-2 text-left hover:opacity-80 transition-opacity"
      >
        <span className="text-micro text-muted-foreground w-3 inline-block pt-1">
          {open ? "▼" : "▶"}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium">{title}</p>
          {subtitle && (
            <p className="text-xs text-muted-foreground mt-0.5">{subtitle}</p>
          )}
        </div>
      </button>
      {headerRight && <div className="shrink-0">{headerRight}</div>}
    </div>
  );
}
