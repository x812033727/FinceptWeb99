import { createContext, useContext } from "react";

export interface PaletteContextValue {
  open: boolean;
  setOpen: (v: boolean) => void;
  toggle: () => void;
}

// Exported so CommandPaletteProvider can populate it; consumers should
// reach for useCommandPalette() instead of the context directly.
export const PaletteContext = createContext<PaletteContextValue | null>(null);

export function useCommandPalette(): PaletteContextValue {
  const ctx = useContext(PaletteContext);
  if (!ctx) {
    throw new Error("useCommandPalette must be used within <CommandPaletteProvider>");
  }
  return ctx;
}
