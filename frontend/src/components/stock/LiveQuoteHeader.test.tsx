/**
 * LiveQuoteHeader tests (blueprint §4.6):
 *   1. initial paint falls back to the REST quote snapshot,
 *   2. first WS tick (via the rAF-batched quoteStore) takes over,
 *   3. selector isolation — a tick for symbol A re-renders only A's
 *      subscriber, never B's (the whole point of the per-key selector).
 *
 * The real authStore has token=null in tests, so useWebSocket's effect
 * no-ops (no socket is opened). Ticks are injected straight into the
 * quoteStore buffer — the WS→buffer wire is covered in
 * useWebSocket.test.ts ("quoteStore routing").
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { LiveQuoteHeader } from "./LiveQuoteHeader";
import { useLiveQuote } from "@/hooks/useWebSocket";
import { bufferQuoteUpdate, _resetQuoteStoreForTests } from "@/store/quoteStore";

// ── manual rAF queue ──────────────────────────────────────────────

let rafQueue: FrameRequestCallback[] = [];

function runFrame(): void {
  const cbs = rafQueue;
  rafQueue = [];
  cbs.forEach((cb) => cb(performance.now()));
}

beforeEach(() => {
  rafQueue = [];
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    rafQueue.push(cb);
    return rafQueue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

afterEach(() => {
  _resetQuoteStoreForTests();
  vi.unstubAllGlobals();
});

const restQuote = {
  price: 180.5,
  change_pct: -1.2,
  name: "Apple Inc.",
  currency: "USD",
  data_source: "stooq",
};

// ── fallback → live transition ────────────────────────────────────

describe("LiveQuoteHeader", () => {
  it("paints the REST quote before any WS tick arrives", () => {
    render(<LiveQuoteHeader symbol="AAPL" market="US" quote={restQuote} isETF={false} />);

    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("Apple Inc.")).toBeInTheDocument();
    expect(screen.getByText("180.50")).toBeInTheDocument();
    expect(screen.getByText("-1.20%")).toBeInTheDocument();
  });

  it("switches to the live quote after the first batched tick", () => {
    render(<LiveQuoteHeader symbol="AAPL" market="US" quote={restQuote} isETF={false} />);
    expect(screen.getByText("180.50")).toBeInTheDocument();

    act(() => {
      bufferQuoteUpdate("AAPL:US", { price: 182.25, change_pct: 0.8 });
      runFrame();
    });

    expect(screen.getByText("182.25")).toBeInTheDocument();
    expect(screen.getByText("+0.80%")).toBeInTheDocument();
    expect(screen.queryByText("180.50")).not.toBeInTheDocument();
  });

  it("renders placeholders (no crash) when there is no REST quote yet", () => {
    render(<LiveQuoteHeader symbol="AAPL" market="US" quote={undefined} isETF={false} />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);

    act(() => {
      bufferQuoteUpdate("AAPL:US", { price: 182.25, change_pct: 0.8 });
      runFrame();
    });
    expect(screen.getByText("182.25")).toBeInTheDocument();
  });
});

// ── useLiveQuote selector isolation ───────────────────────────────

// Module-level render tally — the react-hooks/immutability rule forbids
// mutating props inside a component, so the probe writes here instead.
const renderCounts: Record<string, number> = {};

function Probe({ symbol }: { symbol: string }) {
  // Render-count probe: intentional render-time side effect, test-only.
  // eslint-disable-next-line react-hooks/immutability
  renderCounts[symbol] = (renderCounts[symbol] ?? 0) + 1;
  const live = useLiveQuote(symbol, "US");
  return <div data-testid={`price-${symbol}`}>{live?.price ?? "none"}</div>;
}

describe("useLiveQuote selector isolation", () => {
  it("a tick for one symbol re-renders only that symbol's subscriber", () => {
    renderCounts.AAPL = 0;
    renderCounts.TSLA = 0;

    render(
      <>
        <Probe symbol="AAPL" />
        <Probe symbol="TSLA" />
      </>,
    );
    expect(screen.getByTestId("price-AAPL").textContent).toBe("none");

    const aaplBefore = renderCounts.AAPL;
    const tslaBefore = renderCounts.TSLA;

    act(() => {
      bufferQuoteUpdate("AAPL:US", { price: 182 });
      runFrame();
    });

    expect(screen.getByTestId("price-AAPL").textContent).toBe("182");
    expect(screen.getByTestId("price-TSLA").textContent).toBe("none");
    expect(renderCounts.AAPL).toBe(aaplBefore + 1);   // subscribed symbol re-rendered
    expect(renderCounts.TSLA).toBe(tslaBefore);       // unrelated symbol untouched

    // and the reverse direction, on top of existing store content
    act(() => {
      bufferQuoteUpdate("TSLA:US", { price: 250 });
      runFrame();
    });
    expect(screen.getByTestId("price-TSLA").textContent).toBe("250");
    expect(renderCounts.AAPL).toBe(aaplBefore + 1);   // AAPL still untouched
  });
});
