import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { useNotificationStore } from "./notificationStore";

function resetStore() {
  useNotificationStore.setState({ alerts: [], unreadCount: 0 });
}

describe("notificationStore", () => {
  beforeEach(() => {
    resetStore();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-15T10:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts empty", () => {
    const s = useNotificationStore.getState();
    expect(s.alerts).toEqual([]);
    expect(s.unreadCount).toBe(0);
  });

  it("addAlert prepends new alert, increments unread, stamps ts", () => {
    useNotificationStore.getState().addAlert({
      id: "a1", symbol: "AAPL", market: "US",
      condition: "above", target_price: 180, current_price: 185,
    });

    const s = useNotificationStore.getState();
    expect(s.alerts).toHaveLength(1);
    expect(s.alerts[0].id).toBe("a1");
    expect(s.alerts[0].ts).toBe(new Date("2024-01-15T10:00:00Z").getTime());
    expect(s.unreadCount).toBe(1);
  });

  it("preserves dynamic trend alert projection and message", () => {
    useNotificationStore.getState().addAlert({
      id: "trend-1", symbol: "AAPL", market: "US",
      condition: "above", condition_type: "trend_cross_above",
      target_price: null, projected_price: 203.5, current_price: 204,
      message: "AAPL crossed above trend line",
    });
    expect(useNotificationStore.getState().alerts[0]).toMatchObject({
      condition_type: "trend_cross_above",
      target_price: null,
      projected_price: 203.5,
      message: "AAPL crossed above trend line",
    });
  });

  it("new alerts are inserted at the top", () => {
    const { addAlert } = useNotificationStore.getState();
    addAlert({ id: "a1", symbol: "AAPL", market: "US", condition: "above", target_price: 1, current_price: 2 });
    addAlert({ id: "a2", symbol: "MSFT", market: "US", condition: "below", target_price: 3, current_price: 1 });

    expect(useNotificationStore.getState().alerts.map((a) => a.id)).toEqual(["a2", "a1"]);
  });

  it("retains at most 50 alerts (drops oldest)", () => {
    const { addAlert } = useNotificationStore.getState();
    for (let i = 0; i < 55; i++) {
      addAlert({
        id: `a${i}`, symbol: "AAPL", market: "US",
        condition: "above", target_price: 1, current_price: 2,
      });
    }
    const s = useNotificationStore.getState();
    expect(s.alerts).toHaveLength(50);
    // newest should be a54 at index 0; a5..a54 retained, a0..a4 dropped
    expect(s.alerts[0].id).toBe("a54");
    expect(s.alerts.find((a) => a.id === "a0")).toBeUndefined();
  });

  it("markAllRead zeroes unreadCount without touching alerts", () => {
    const { addAlert, markAllRead } = useNotificationStore.getState();
    addAlert({ id: "a1", symbol: "AAPL", market: "US", condition: "above", target_price: 1, current_price: 2 });
    addAlert({ id: "a2", symbol: "MSFT", market: "US", condition: "below", target_price: 1, current_price: 0 });

    markAllRead();
    const s = useNotificationStore.getState();
    expect(s.unreadCount).toBe(0);
    expect(s.alerts).toHaveLength(2);
  });

  it("dismiss removes the matching alert", () => {
    const { addAlert, dismiss } = useNotificationStore.getState();
    addAlert({ id: "a1", symbol: "AAPL", market: "US", condition: "above", target_price: 1, current_price: 2 });
    addAlert({ id: "a2", symbol: "MSFT", market: "US", condition: "below", target_price: 1, current_price: 0 });

    dismiss("a1");
    const remaining = useNotificationStore.getState().alerts.map((a) => a.id);
    expect(remaining).toEqual(["a2"]);
  });

  it("dismiss of unknown id is a no-op", () => {
    useNotificationStore.getState().addAlert({
      id: "a1", symbol: "AAPL", market: "US",
      condition: "above", target_price: 1, current_price: 2,
    });
    useNotificationStore.getState().dismiss("nonexistent");
    expect(useNotificationStore.getState().alerts).toHaveLength(1);
  });
});
