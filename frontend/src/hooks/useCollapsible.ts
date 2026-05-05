import { useEffect, useState } from "react";

// Per-card collapse state, persisted to localStorage so users keep
// their layout preference across reloads / new tabs. Pair with
// `<CollapsibleHeader>` from components/Collapsible — useCollapsible
// returns `{ open, toggle }`, the header takes both as props.
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
