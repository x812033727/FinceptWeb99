import { ReactNode, useEffect, useState } from "react";

/**
 * Per-card collapse state, persisted to localStorage so users keep
 * their layout preference across reloads / new tabs. Originally lived
 * in `AdminPage` (PR #153 made every admin card collapsible); pulled
 * out here so DiscussionPage / StockDetailPage / PortfolioPage's
 * future card extractions can reuse it without copy-paste.
 *
 * Pair with `<CollapsibleHeader>` below — `useCollapsible` returns
 * `{ open, toggle }`, the header takes both as props.
 */
export function useCollapsible(storageKey: string, defaultOpen = true) {
  const [open, setOpen] = useState<boolean>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw === "0") return false;
      if (raw === "1") return true;
    } catch {
      /* private mode / quota — fall through to default */
    }
    return defaultOpen;
  });
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, open ? "1" : "0");
    } catch {
      /* see above */
    }
  }, [storageKey, open]);
  return { open, toggle: () => setOpen((v) => !v) };
}

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
        <span className="text-[10px] text-muted-foreground w-3 inline-block pt-1">
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
