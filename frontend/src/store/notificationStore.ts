import { create } from "zustand";
import { useAuthStore } from "./authStore";

export interface AlertNotification {
  id: string;
  symbol: string;
  market: string;
  condition: "above" | "below" | null;
  condition_type?: string;
  target_price: number | null;
  projected_price?: number;
  current_price?: number;
  message?: string;
  ts: number;
}

interface NotificationState {
  alerts: AlertNotification[];
  unreadCount: number;
  addAlert: (alert: Omit<AlertNotification, "ts">) => void;
  markAllRead: () => void;
  dismiss: (id: string) => void;
  clear: () => void;
}

export const useNotificationStore = create<NotificationState>((set) => ({
  alerts: [],
  unreadCount: 0,
  addAlert: (alert) =>
    set((s) => ({
      alerts: [{ ...alert, ts: Date.now() }, ...s.alerts].slice(0, 50),
      unreadCount: s.unreadCount + 1,
    })),
  markAllRead: () => set({ unreadCount: 0 }),
  dismiss: (id) =>
    set((s) => ({ alerts: s.alerts.filter((a) => a.id !== id) })),
  clear: () => set({ alerts: [], unreadCount: 0 }),
}));

/** Prevent realtime alerts from surviving logout or account replacement. */
export function clearNotificationsOnAuthChange() {
  let userId = useAuthStore.getState().user?.id ?? null;
  return useAuthStore.subscribe((state) => {
    const nextUserId = state.user?.id ?? null;
    if (nextUserId === userId) return;
    userId = nextUserId;
    useNotificationStore.getState().clear();
  });
}
