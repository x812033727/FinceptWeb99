/**
 * Unit tests for useWebSocket / useAlertSocket / useWsConnected.
 *
 * The module uses a module-level singleton (socket, subscribers, etc.).
 * We stub window.WebSocket with a FakeWebSocket that lets tests inject
 * messages and inspect outbound sends without a real network.
 *
 * vi.resetModules() + dynamic import gives each describe block a fresh
 * module instance so singleton state never bleeds across test groups.
 */
import { renderHook, act } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";

// ── Fake WebSocket ────────────────────────────────────────────────
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  static CLOSED = 3;

  readyState = FakeWebSocket.OPEN;
  sent: string[] = [];

  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  // Helpers used in tests
  triggerOpen() { this.onopen?.(); }
  triggerMessage(data: object) { this.onmessage?.({ data: JSON.stringify(data) }); }
  triggerClose() { this.close(); }
}

function resetFake() {
  FakeWebSocket.instances = [];
}

// Inject fake into global and mock authStore at the top level so Vitest
// hoisting works correctly. Both must be declared before any test runs.
vi.stubGlobal("WebSocket", FakeWebSocket);

vi.mock("@/store/authStore", () => ({
  useAuthStore: (selector: (s: { token: string | null }) => unknown) =>
    selector({ token: "test-token" }),
}));

// ── helpers ────────────────────────────────────────────────────────

/** Import fresh module to get clean singleton state. */
async function freshModule() {
  vi.resetModules();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  return import("./useWebSocket");
}

function latestSocket(): FakeWebSocket {
  return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

// ── connect / auth handshake ───────────────────────────────────────

describe("connection lifecycle", () => {
  beforeEach(resetFake);
  afterEach(() => vi.resetModules());

  it("sends auth JSON on open", async () => {
    const { useWebSocket } = await freshModule();

    renderHook(() => useWebSocket("AAPL:US", () => {}));
    const ws = latestSocket();
    act(() => ws.triggerOpen());

    const authMsg = JSON.parse(ws.sent[0]);
    expect(authMsg).toEqual({ action: "auth", token: "test-token" });
  });

  it("sends subscribe after ping", async () => {
    const { useWebSocket } = await freshModule();

    renderHook(() => useWebSocket("MSFT:US", () => {}));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({ type: "ping" }));

    // Should have sent: auth, pong, subscribe
    const msgs = ws.sent.map((s) => JSON.parse(s));
    const pong = msgs.find((m) => m.action === "pong");
    const sub = msgs.find((m) => m.action === "subscribe");
    expect(pong).toBeDefined();
    expect(sub).toBeDefined();
    expect(sub.symbols).toContain("MSFT");
    expect(sub.markets).toContain("US");
  });

  it("does not open a second socket when already open", async () => {
    const { useWebSocket } = await freshModule();

    renderHook(() => useWebSocket("AAPL:US", () => {}));
    renderHook(() => useWebSocket("TSLA:US", () => {}));

    // Only one WebSocket should be created
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

// ── delta message routing ──────────────────────────────────────────

describe("delta message routing", () => {
  beforeEach(resetFake);
  afterEach(() => vi.resetModules());

  it("dispatches delta to matching subscriber", async () => {
    const { useWebSocket } = await freshModule();

    const received: unknown[] = [];
    renderHook(() => useWebSocket("AAPL:US", (d) => received.push(d)));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({ type: "delta", symbol: "AAPL", market: "US", data: { price: 182 } }));

    expect(received).toHaveLength(1);
    expect((received[0] as any).price).toBe(182);
  });

  it("does not dispatch delta to unrelated subscriber", async () => {
    const { useWebSocket } = await freshModule();

    const received: unknown[] = [];
    renderHook(() => useWebSocket("TSLA:US", (d) => received.push(d)));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({ type: "delta", symbol: "AAPL", market: "US", data: { price: 182 } }));

    expect(received).toHaveLength(0);
  });

  it("dispatches snapshot data to all matching subscribers", async () => {
    const { useWebSocket } = await freshModule();

    const aapl: unknown[] = [];
    const tsla: unknown[] = [];
    renderHook(() => useWebSocket("AAPL:US", (d) => aapl.push(d)));
    renderHook(() => useWebSocket("TSLA:US", (d) => tsla.push(d)));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({
      type: "snapshot",
      data: {
        "AAPL:US": { price: 180 },
        "TSLA:US": { price: 250 },
      },
    }));

    expect(aapl).toHaveLength(1);
    expect(tsla).toHaveLength(1);
    expect((aapl[0] as any).price).toBe(180);
    expect((tsla[0] as any).price).toBe(250);
  });
});

// ── alert routing ──────────────────────────────────────────────────

describe("useAlertSocket", () => {
  beforeEach(resetFake);
  afterEach(() => vi.resetModules());

  it("routes alert messages to alert subscribers", async () => {
    const { useAlertSocket } = await freshModule();

    const alerts: unknown[] = [];
    renderHook(() => useAlertSocket((a) => alerts.push(a)));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({
      type: "alert",
      id: "a1",
      symbol: "NVDA",
      market: "US",
      condition: "above",
      target_price: 900,
      current_price: 910,
    }));

    expect(alerts).toHaveLength(1);
    expect((alerts[0] as any).symbol).toBe("NVDA");
    expect((alerts[0] as any).condition).toBe("above");
  });

  it("does not dispatch alert to regular symbol subscribers", async () => {
    const { useWebSocket, useAlertSocket } = await freshModule();

    const symbolCbs: unknown[] = [];
    const alertCbs: unknown[] = [];
    renderHook(() => useWebSocket("NVDA:US", (d) => symbolCbs.push(d)));
    renderHook(() => useAlertSocket((a) => alertCbs.push(a)));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({
      type: "alert",
      id: "a2",
      symbol: "NVDA",
      market: "US",
      condition: "above",
      target_price: 900,
      current_price: 910,
    }));

    expect(symbolCbs).toHaveLength(0);  // symbol callback NOT called for alerts
    expect(alertCbs).toHaveLength(1);
  });
});

// ── useWsConnected ─────────────────────────────────────────────────

describe("useWsConnected", () => {
  beforeEach(resetFake);
  afterEach(() => vi.resetModules());

  it("reflects connected=true after socket opens", async () => {
    const { useWebSocket, useWsConnected } = await freshModule();

    const { result: connResult } = renderHook(() => useWsConnected());
    renderHook(() => useWebSocket("AMD:US", () => {}));

    const ws = latestSocket();
    act(() => ws.triggerOpen());

    expect(connResult.current).toBe(true);
  });

  it("reflects connected=false after socket closes", async () => {
    const { useWebSocket, useWsConnected } = await freshModule();

    const { result: connResult } = renderHook(() => useWsConnected());
    renderHook(() => useWebSocket("AMD:US", () => {}));

    const ws = latestSocket();
    act(() => ws.triggerOpen());
    expect(connResult.current).toBe(true);

    act(() => ws.triggerClose());
    expect(connResult.current).toBe(false);
  });
});

// ── cleanup / unsubscribe ──────────────────────────────────────────

describe("cleanup", () => {
  beforeEach(resetFake);
  afterEach(() => vi.resetModules());

  it("stops delivering messages after unmount", async () => {
    const { useWebSocket } = await freshModule();

    const received: unknown[] = [];
    const { unmount } = renderHook(() => useWebSocket("AAPL:US", (d) => received.push(d)));
    const ws = latestSocket();

    act(() => ws.triggerOpen());
    act(() => ws.triggerMessage({ type: "delta", symbol: "AAPL", market: "US", data: { price: 182 } }));
    expect(received).toHaveLength(1);

    unmount();

    act(() => ws.triggerMessage({ type: "delta", symbol: "AAPL", market: "US", data: { price: 183 } }));
    expect(received).toHaveLength(1);  // no new message after unmount
  });
});
