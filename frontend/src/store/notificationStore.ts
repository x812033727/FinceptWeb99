import { create } from "zustand";

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
}));
