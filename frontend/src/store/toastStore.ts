import { create } from "zustand";

export type ToastSeverity = "info" | "warning" | "error" | "success";

export interface Toast {
  id: string;
  severity: ToastSeverity;
  title: string;
  detail?: string;
  ttlMs: number;
  ts: number;
}

interface ToastState {
  toasts: Toast[];
  push: (toast: Omit<Toast, "id" | "ts" | "ttlMs"> & { ttlMs?: number }) => string;
  dismiss: (id: string) => void;
  clear: () => void;
}

const DEFAULT_TTL_MS = 6000;
let counter = 0;

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: (toast) => {
    counter += 1;
    const id = `t${Date.now()}-${counter}`;
    set((s) => ({
      toasts: [
        { ...toast, id, ts: Date.now(), ttlMs: toast.ttlMs ?? DEFAULT_TTL_MS },
        ...s.toasts,
      ].slice(0, 8),
    }));
    return id;
  },
  dismiss: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  clear: () => set({ toasts: [] }),
}));
