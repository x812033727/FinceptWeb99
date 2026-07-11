/**
 * quoteStore rAF batching tests (blueprint §4.6).
 *
 * requestAnimationFrame is stubbed with a manual queue so the tests
 * control exactly when a "frame" fires; the document.hidden fallback
 * path runs under vi.useFakeTimers.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  useQuoteStore,
  bufferQuoteUpdate,
  _resetQuoteStoreForTests,
} from "./quoteStore";

// ── manual rAF queue ──────────────────────────────────────────────

let rafQueue: FrameRequestCallback[] = [];

function runFrame(): void {
  const cbs = rafQueue;
  rafQueue = [];
  cbs.forEach((cb) => cb(performance.now()));
}

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "hidden", {
    value: hidden,
    configurable: true,
  });
}

beforeEach(() => {
  rafQueue = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    rafQueue.push(cb);
    return rafQueue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  setHidden(false);
});

afterEach(() => {
  _resetQuoteStoreForTests();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setHidden(false);
});

// ── buffering / flush ─────────────────────────────────────────────

describe("quoteStore rAF batching", () => {
  it("does not touch the store until the animation frame fires", () => {
    bufferQuoteUpdate("AAPL:US", { price: 182, change_pct: 1.2 });

    expect(useQuoteStore.getState().quotes["AAPL:US"]).toBeUndefined();

    runFrame();

    const q = useQuoteStore.getState().quotes["AAPL:US"];
    expect(q.price).toBe(182);
    expect(q.changePct).toBe(1.2);
  });

  it("coalesces multiple deltas for the same symbol within one frame", () => {
    bufferQuoteUpdate("AAPL:US", { price: 181, volume: 100 });
    bufferQuoteUpdate("AAPL:US", { price: 182 });
    bufferQuoteUpdate("AAPL:US", { change_pct: 0.9 });

    const listener = vi.fn();
    const unsub = useQuoteStore.subscribe(listener);
    runFrame();
    unsub();

    // One frame = exactly one zustand set()
    expect(listener).toHaveBeenCalledTimes(1);

    const q = useQuoteStore.getState().quotes["AAPL:US"];
    expect(q.price).toBe(182);       // last write wins
    expect(q.volume).toBe(100);      // earlier field survives the merge
    expect(q.changePct).toBe(0.9);
  });

  it("flushes many symbols in a single set()", () => {
    bufferQuoteUpdate("AAPL:US", { price: 182 });
    bufferQuoteUpdate("TSLA:US", { price: 250 });
    bufferQuoteUpdate("2330:TW", { price: 1050 });

    const listener = vi.fn();
    const unsub = useQuoteStore.subscribe(listener);
    runFrame();
    unsub();

    expect(listener).toHaveBeenCalledTimes(1);
    const { quotes } = useQuoteStore.getState();
    expect(quotes["AAPL:US"].price).toBe(182);
    expect(quotes["TSLA:US"].price).toBe(250);
    expect(quotes["2330:TW"].price).toBe(1050);
  });

  it("preserves referential identity of untouched symbols across flushes", () => {
    bufferQuoteUpdate("AAPL:US", { price: 182 });
    bufferQuoteUpdate("TSLA:US", { price: 250 });
    runFrame();

    const before = useQuoteStore.getState().quotes;
    bufferQuoteUpdate("AAPL:US", { price: 183 });
    runFrame();
    const after = useQuoteStore.getState().quotes;

    expect(after["AAPL:US"]).not.toBe(before["AAPL:US"]);
    expect(after["AAPL:US"].price).toBe(183);
    // untouched symbol keeps its object — per-key selectors skip re-render
    expect(after["TSLA:US"]).toBe(before["TSLA:US"]);
  });

  it("merges new ticks into the existing record across frames", () => {
    bufferQuoteUpdate("AAPL:US", { price: 182, data_source: "polygon", ts: 1 });
    runFrame();
    bufferQuoteUpdate("AAPL:US", { change_pct: -0.4 });
    runFrame();

    const q = useQuoteStore.getState().quotes["AAPL:US"];
    expect(q.price).toBe(182);          // survives from frame 1
    expect(q.dataSource).toBe("polygon");
    expect(q.ts).toBe(1);
    expect(q.changePct).toBe(-0.4);
  });

  it("ignores placeholder price 0 and malformed payloads", () => {
    bufferQuoteUpdate("AAPL:US", { price: 182 });
    runFrame();

    bufferQuoteUpdate("AAPL:US", { price: 0 });          // upstream-down placeholder
    bufferQuoteUpdate("AAPL:US", "not-an-object");
    bufferQuoteUpdate("AAPL:US", null);
    bufferQuoteUpdate("AAPL:US", { unknown_field: 1 });  // nothing usable → no schedule
    runFrame();

    expect(useQuoteStore.getState().quotes["AAPL:US"].price).toBe(182);
  });
});

// ── hidden-tab fallback ───────────────────────────────────────────

describe("document.hidden fallback", () => {
  it("flushes via ~100ms timer when the tab is hidden (rAF parked)", () => {
    // Fake ONLY timeout timers — vitest's default fake-timer set also
    // fakes requestAnimationFrame, which would override the manual rAF
    // queue this suite controls.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    setHidden(true);

    bufferQuoteUpdate("AAPL:US", { price: 182 });

    // No rAF scheduled while hidden
    expect(rafQueue).toHaveLength(0);
    expect(useQuoteStore.getState().quotes["AAPL:US"]).toBeUndefined();

    vi.advanceTimersByTime(100);

    expect(useQuoteStore.getState().quotes["AAPL:US"].price).toBe(182);
  });

  it("timer safety-net drains the buffer even if a scheduled rAF never fires", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    setHidden(false);

    bufferQuoteUpdate("AAPL:US", { price: 182 });
    expect(rafQueue).toHaveLength(1);

    // Simulate the tab hiding right after scheduling: the rAF callback
    // is parked forever, but the fallback timer still flushes.
    vi.advanceTimersByTime(100);

    expect(useQuoteStore.getState().quotes["AAPL:US"].price).toBe(182);
  });
});
