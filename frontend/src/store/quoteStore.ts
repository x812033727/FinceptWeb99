/**
 * Live quote store — rAF-batched sink for WebSocket ticks (blueprint §4.6).
 *
 * Problem it solves: every WS delta used to trigger several `setState`
 * calls inside the subscribing page, re-rendering the whole page (chart
 * container included) on every tick. Multi-row pages re-rendered every
 * row on any symbol's tick.
 *
 * Design:
 *   - State is keyed by `"SYMBOL:MARKET"` (same key format as
 *     `useWebSocket`), one `LiveQuote` record per instrument.
 *   - Incoming deltas do NOT call `set()` directly. They accumulate in a
 *     module-level buffer; one flush per animation frame merges every
 *     buffered symbol in a single zustand `set()`. A burst of N ticks in
 *     one frame therefore costs exactly one store update.
 *   - Background tabs: rAF is parked while `document.hidden`, so a
 *     ~100 ms timeout fallback keeps quotes flowing (the browser may
 *     throttle it further — that's fine, it's a freshness floor, not a
 *     frame budget). The timer is also armed alongside rAF as a safety
 *     net for the "tab hides right after scheduling" race; whichever
 *     fires first flushes and cancels the other.
 *   - Flush preserves referential identity of untouched symbols, so a
 *     per-key selector (`useQuote`) only re-renders components whose own
 *     symbol actually changed.
 *
 * Components should normally consume this via `useLiveQuote` from
 * `@/hooks/useWebSocket`, which also registers the WS subscription.
 */
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

export interface LiveQuote {
  price: number | null;
  change: number | null;
  changePct: number | null;
  volume: number | null;
  /** Quote timestamp, epoch ms (backend `_normalize_quote` shape). */
  ts: number | null;
  /** Upstream provider of the latest tick, e.g. "twse" | "finmind". */
  dataSource: string | null;
}

interface QuoteState {
  quotes: Record<string, LiveQuote>;
}

const EMPTY_QUOTE: LiveQuote = {
  price: null,
  change: null,
  changePct: null,
  volume: null,
  ts: null,
  dataSource: null,
};

export const useQuoteStore = create<QuoteState>(() => ({ quotes: {} }));

// ── rAF-batched write buffer (module-level, shared by all consumers) ──

const buffer = new Map<string, Partial<LiveQuote>>();
let rafId: number | null = null;
let fallbackTimer: ReturnType<typeof setTimeout> | null = null;

/** Freshness floor for hidden tabs where rAF never fires. */
const HIDDEN_FLUSH_MS = 100;

function cancelScheduled(): void {
  if (rafId !== null) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
  if (fallbackTimer !== null) {
    clearTimeout(fallbackTimer);
    fallbackTimer = null;
  }
}

/** Merge every buffered symbol into the store in ONE zustand set(). */
function flush(): void {
  cancelScheduled();
  if (buffer.size === 0) return;

  const prev = useQuoteStore.getState().quotes;
  const patch: Record<string, LiveQuote> = {};
  for (const [key, partial] of buffer) {
    patch[key] = { ...EMPTY_QUOTE, ...prev[key], ...partial };
  }
  buffer.clear();
  // Spread keeps untouched symbols referentially identical, which is what
  // lets per-key selectors skip re-rendering unrelated components.
  useQuoteStore.setState((s) => ({ quotes: { ...s.quotes, ...patch } }));
}

function scheduleFlush(): void {
  const hidden = typeof document !== "undefined" && document.hidden;
  if (!hidden && rafId === null) {
    rafId = requestAnimationFrame(flush);
  }
  // Armed in both visibility states: primary mechanism when hidden,
  // safety net when visible (a tab hidden between schedule and paint
  // parks the rAF indefinitely; the timer still drains the buffer).
  if (fallbackTimer === null) {
    fallbackTimer = setTimeout(flush, HIDDEN_FLUSH_MS);
  }
}

/**
 * Queue a raw WS quote payload (snapshot entry or delta `data`) for the
 * next batched flush. Field names follow the backend wire format
 * (`change_pct`, `data_source`); unknown/malformed fields are ignored.
 *
 * @param key `"SYMBOL:MARKET"`, e.g. `"2330:TW"`.
 */
export function bufferQuoteUpdate(key: string, raw: unknown): void {
  if (!raw || typeof raw !== "object") return;
  const d = raw as Record<string, unknown>;

  const partial: Partial<LiveQuote> = {};
  // `price: 0` is a placeholder the backend emits when an upstream is
  // down — never overwrite a real price with it (same guard the old
  // StockDetailPage setState path had).
  if (typeof d.price === "number" && d.price) partial.price = d.price;
  if (typeof d.change === "number") partial.change = d.change;
  if (typeof d.change_pct === "number") partial.changePct = d.change_pct;
  if (typeof d.volume === "number") partial.volume = d.volume;
  if (typeof d.ts === "number") partial.ts = d.ts;
  if (typeof d.data_source === "string") partial.dataSource = d.data_source;
  if (Object.keys(partial).length === 0) return;

  buffer.set(key, { ...buffer.get(key), ...partial });
  scheduleFlush();
}

/**
 * Selector hook: subscribe to ONE instrument's live quote. Re-renders
 * only when that key's record changes (untouched keys keep identity
 * across flushes; `useShallow` additionally guards against equal-value
 * rebuilds). Returns `undefined` until the first tick arrives —
 * callers fall back to their REST snapshot for initial paint.
 *
 * NOTE: this is the pure store read. It does NOT register a WS
 * subscription — use `useLiveQuote` from `@/hooks/useWebSocket` in
 * components so the backend actually streams the symbol.
 */
export function useQuote(key: string): LiveQuote | undefined {
  return useQuoteStore(useShallow((s) => s.quotes[key]));
}

/** Test-only: clear buffer, cancel pending flushes, empty the store. */
export function _resetQuoteStoreForTests(): void {
  cancelScheduled();
  buffer.clear();
  useQuoteStore.setState({ quotes: {} });
}
