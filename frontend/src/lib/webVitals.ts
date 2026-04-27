/**
 * Core Web Vitals reporter — registers callbacks for LCP / INP / CLS
 * / FCP / TTFB and beacons each measurement to /api/system/web-vital.
 *
 * Implementation notes:
 *
 *   - Uses `navigator.sendBeacon` when available so the request still
 *     fires after the user navigates away or backgrounds the tab —
 *     critical for INP and CLS, which can be reported very late
 *     in the page lifecycle.
 *   - Falls back to a `fetch` POST with `keepalive: true` if
 *     sendBeacon is unavailable (some older Safari builds).
 *   - The endpoint is anonymous on purpose; we don't attach the
 *     auth token. See backend api/system/router.py for rationale.
 *   - Failures are swallowed — analytics that breaks the page is
 *     worse than no analytics.
 */
import { onCLS, onFCP, onINP, onLCP, onTTFB, type Metric } from "web-vitals";

interface VitalReport {
  name: Metric["name"];
  value: number;
  path: string;
}

const ENDPOINT = "/api/system/web-vital";

function send(report: VitalReport): void {
  // sendBeacon needs a Blob with the right Content-Type so the
  // backend's JSON parser picks it up.
  const body = JSON.stringify(report);
  if (typeof navigator.sendBeacon === "function") {
    const blob = new Blob([body], { type: "application/json" });
    if (navigator.sendBeacon(ENDPOINT, blob)) return;
  }
  // Fallback. keepalive lets the browser finish the request even
  // after the page unloads.
  void fetch(ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => {
    // Swallow — see file header.
  });
}

function handle(metric: Metric): void {
  // CLS comes back unitless; the backend already differentiates on
  // metric name and routes to the right histogram, so we just forward
  // the raw value.
  send({
    name: metric.name,
    value: metric.value,
    // window.location.pathname is path-only, not the full URL —
    // backend normalizes it down to one of ~13 known route shapes.
    path: window.location.pathname || "/",
  });
}

let registered = false;

/** Register web-vitals callbacks. Idempotent — safe to call from
 *  StrictMode's double-invoke render. */
export function registerWebVitals(): void {
  if (registered) return;
  registered = true;
  onLCP(handle);
  onINP(handle);
  onCLS(handle);
  onFCP(handle);
  onTTFB(handle);
}
