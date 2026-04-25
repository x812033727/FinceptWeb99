import { describe, it, expect, beforeEach } from "vitest";
import { useToastStore } from "./toastStore";

function reset() {
  useToastStore.setState({ toasts: [] });
}

describe("toastStore", () => {
  beforeEach(reset);

  it("starts empty", () => {
    expect(useToastStore.getState().toasts).toEqual([]);
  });

  it("push prepends a toast and returns its id", () => {
    const id = useToastStore.getState().push({
      severity: "info",
      title: "hi",
      detail: "world",
    });
    const s = useToastStore.getState();
    expect(s.toasts).toHaveLength(1);
    expect(s.toasts[0].id).toBe(id);
    expect(s.toasts[0].severity).toBe("info");
    expect(s.toasts[0].title).toBe("hi");
    expect(s.toasts[0].ttlMs).toBe(6000);
  });

  it("respects custom ttlMs", () => {
    useToastStore.getState().push({
      severity: "warning", title: "slow", ttlMs: 1234,
    });
    expect(useToastStore.getState().toasts[0].ttlMs).toBe(1234);
  });

  it("caps queue at 8 toasts (drops oldest)", () => {
    for (let i = 0; i < 12; i++) {
      useToastStore.getState().push({ severity: "info", title: `t${i}` });
    }
    const titles = useToastStore.getState().toasts.map((t) => t.title);
    expect(titles).toHaveLength(8);
    expect(titles[0]).toBe("t11");
    expect(titles[7]).toBe("t4");
  });

  it("dismiss removes the matching toast", () => {
    const id1 = useToastStore.getState().push({ severity: "info", title: "a" });
    useToastStore.getState().push({ severity: "info", title: "b" });
    useToastStore.getState().dismiss(id1);
    const titles = useToastStore.getState().toasts.map((t) => t.title);
    expect(titles).toEqual(["b"]);
  });

  it("clear empties the queue", () => {
    useToastStore.getState().push({ severity: "info", title: "x" });
    useToastStore.getState().push({ severity: "error", title: "y" });
    useToastStore.getState().clear();
    expect(useToastStore.getState().toasts).toEqual([]);
  });
});
