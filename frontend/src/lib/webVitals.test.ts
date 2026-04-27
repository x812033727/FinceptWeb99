/**
 * Tests for the Core Web Vitals reporter.
 *
 * The web-vitals package is mocked to capture the handler that
 * registerWebVitals() registers, then we drive that handler with
 * synthetic Metric payloads and assert on what hits sendBeacon /
 * fetch.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const handlers: Record<string, (m: unknown) => void> = {};

vi.mock("web-vitals", () => ({
  onCLS: (cb: (m: unknown) => void) => { handlers.CLS = cb; },
  onFCP: (cb: (m: unknown) => void) => { handlers.FCP = cb; },
  onINP: (cb: (m: unknown) => void) => { handlers.INP = cb; },
  onLCP: (cb: (m: unknown) => void) => { handlers.LCP = cb; },
  onTTFB: (cb: (m: unknown) => void) => { handlers.TTFB = cb; },
}));

describe("registerWebVitals", () => {
  let beaconMock: ReturnType<typeof vi.fn>;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    vi.resetModules();
    Object.keys(handlers).forEach((k) => delete handlers[k]);

    beaconMock = vi.fn().mockReturnValue(true);
    fetchMock = vi.fn().mockResolvedValue({});
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      writable: true,
      value: beaconMock,
    });
    vi.stubGlobal("fetch", fetchMock);

    // Stable pathname so URL-related assertions are deterministic.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/stock/US/AAPL" },
    });

    const { registerWebVitals } = await import("./webVitals");
    registerWebVitals();
  });

  it("registers a handler with each web-vitals callback", () => {
    expect(Object.keys(handlers).sort()).toEqual(
      ["CLS", "FCP", "INP", "LCP", "TTFB"]
    );
  });

  it("forwards LCP via sendBeacon with the right shape", () => {
    handlers.LCP({ name: "LCP", value: 1.42 });
    expect(beaconMock).toHaveBeenCalledTimes(1);
    const [url, blob] = beaconMock.mock.calls[0];
    expect(url).toBe("/api/system/web-vital");
    // sendBeacon argument is a Blob — read it back via FileReader-
    // style sync conversion is awkward; use Blob.text() (jsdom
    // supports it).
    return (blob as Blob).text().then((body) => {
      expect(JSON.parse(body)).toEqual({
        name: "LCP",
        value: 1.42,
        path: "/stock/US/AAPL",
      });
    });
  });

  it("forwards CLS unitless score the same way", () => {
    handlers.CLS({ name: "CLS", value: 0.08 });
    expect(beaconMock).toHaveBeenCalledTimes(1);
    return (beaconMock.mock.calls[0][1] as Blob).text().then((body) => {
      expect(JSON.parse(body)).toMatchObject({ name: "CLS", value: 0.08 });
    });
  });

  it("falls back to fetch with keepalive when sendBeacon is unavailable", async () => {
    // Re-register without sendBeacon. resetModules so the singleton's
    // `registered` flag flips off.
    vi.resetModules();
    Object.keys(handlers).forEach((k) => delete handlers[k]);
    Object.defineProperty(navigator, "sendBeacon", {
      configurable: true,
      writable: true,
      value: undefined,
    });
    const { registerWebVitals } = await import("./webVitals");
    registerWebVitals();

    handlers.INP({ name: "INP", value: 0.12 });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/system/web-vital");
    expect(init.method).toBe("POST");
    expect(init.keepalive).toBe(true);
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toMatchObject({ name: "INP", value: 0.12 });
  });

  it("is idempotent — second call does not double-register", async () => {
    // First call already happened in beforeEach. Capture handler ID
    // then call again and ensure the same handler is still in place
    // (i.e. the second call was a no-op, not a re-registration).
    const firstHandler = handlers.LCP;
    const { registerWebVitals } = await import("./webVitals");
    registerWebVitals();
    expect(handlers.LCP).toBe(firstHandler);
  });
});
