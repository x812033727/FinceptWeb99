/**
 * Manages a single WebSocket connection per user session.
 * Components call subscribe(key, cb) / unsubscribe(key, cb) in useEffect.
 *
 * Reconnect: exponential backoff 1s → 2s → 4s → max 30s, with ±25% jitter
 * so a deploy / network blip that drops every client at once doesn't make
 * them all retry on the same tick (thundering-herd → backend overload).
 *
 * key format: "SYMBOL:MARKET"  e.g. "AAPL:US" | "2330:TW"
 */
import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "@/store/authStore";
import { bufferQuoteUpdate, useQuote, type LiveQuote } from "@/store/quoteStore";

type Callback = (data: unknown) => void;

export type AlertMessage = {
  type: "alert";
  id: string;
  symbol: string;
  market: string;
  condition: "above" | "below";
  target_price: number;
  current_price: number;
};
type AlertCallback = (alert: AlertMessage) => void;

const subscribers = new Map<string, Set<Callback>>();
const alertCallbacks = new Set<AlertCallback>();
const wsStateListeners = new Set<(connected: boolean) => void>();
let socket: WebSocket | null = null;
let reconnectDelay = 1000;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let authAckTimer: ReturnType<typeof setTimeout> | null = null;
let authenticated = false;
let wsConnected = false;

// Server expects an auth message within ~5s of the WebSocket opening; we
// give it the same window to ack before tearing down. If the server never
// responds (network half-open, broken proxy), the close+reconnect loop
// will keep trying with backoff.
const AUTH_ACK_TIMEOUT_MS = 7_000;

function notifyWsState(connected: boolean): void {
  wsConnected = connected;
  wsStateListeners.forEach((cb) => cb(connected));
}

function getWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/market`;
}

function sendJson(data: unknown): void {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(data));
  }
}

function subscribeSymbols(): void {
  const keys = [...subscribers.keys()].filter((k) => subscribers.get(k)?.size);
  if (!keys.length) return;
  const symbols = [...new Set(keys.map((k) => k.split(":")[0]))];
  const markets  = [...new Set(keys.map((k) => k.split(":")[1]))];
  sendJson({ action: "subscribe", symbols, markets });
}

function reauth(token: string): void {
  // Backend WS now enforces the JWT exp on every message. When the
  // 15-min access token rotates, push the new credential over the
  // existing socket instead of forcing a full reconnect.
  if (socket?.readyState === WebSocket.OPEN && token) {
    sendJson({ action: "auth", token });
  }
}

function connect(token: string): void {
  if (!token) return;
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    reauth(token);
    return;
  }

  socket = new WebSocket(getWsUrl());
  authenticated = false;

  socket.onopen = () => {
    reconnectDelay = 1000;
    notifyWsState(true);
    // Re-read the latest token in case it rotated during connect()
    const liveToken = useAuthStore.getState().token;
    if (!liveToken) {
      socket?.close();
      return;
    }
    sendJson({ action: "auth", token: liveToken });
    if (authAckTimer) clearTimeout(authAckTimer);
    authAckTimer = setTimeout(() => {
      if (!authenticated && socket && socket.readyState !== WebSocket.CLOSED) {
        socket.close(4002, "auth ack timeout");
      }
    }, AUTH_ACK_TIMEOUT_MS);
  };

  socket.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data as string);

      if (msg.type === "snapshot") {
        authenticated = true;
        for (const [key, data] of Object.entries(msg.data ?? {})) {
          // Feed the rAF-batched quoteStore in addition to the legacy
          // per-key callbacks; consumers migrate to useLiveQuote
          // gradually (blueprint §4.6).
          bufferQuoteUpdate(key, data);
          subscribers.get(key)?.forEach((cb) => cb(data));
        }
        return;
      }

      if (msg.type === "delta") {
        const key = `${msg.symbol}:${msg.market}`;
        bufferQuoteUpdate(key, msg.data);
        subscribers.get(key)?.forEach((cb) => cb(msg.data));
        return;
      }

      if (msg.type === "ping") {
        sendJson({ action: "pong" });
        if (!authenticated) {
          authenticated = true;
          subscribeSymbols();
        }
        return;
      }

      if (msg.type === "alert") {
        alertCallbacks.forEach((cb) => cb(msg as AlertMessage));
        return;
      }

      if (msg.type === "auth_ok") {
        // Server accepted re-auth on an already-open socket. Subscriptions
        // remain intact; nothing to do.
        return;
      }
    } catch {
      // ignore parse errors
    }
  };

  socket.onclose = () => {
    authenticated = false;
    socket = null;
    if (authAckTimer) {
      clearTimeout(authAckTimer);
      authAckTimer = null;
    }
    notifyWsState(false);
    if (reconnectTimer) clearTimeout(reconnectTimer);
    // ±25% jitter on the scheduled delay so a deploy that drops every
    // client at once doesn't make them all retry on the same millisecond.
    const jittered = reconnectDelay * (0.75 + Math.random() * 0.5);
    reconnectTimer = setTimeout(() => {
      const t = useAuthStore.getState().token;
      if (t) connect(t);
    }, jittered);
    reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
  };

  socket.onerror = () => socket?.close();
}

function disconnect(): void {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (authAckTimer) {
    clearTimeout(authAckTimer);
    authAckTimer = null;
  }
  socket?.close();
  socket = null;
  authenticated = false;
  notifyWsState(false);
}

// ── React hooks ───────────────────────────────────────────────────

export function useWebSocket(key: string, callback: Callback): void {
  const token = useAuthStore((s) => s.token);
  const cbRef = useRef(callback);
  // eslint-disable-next-line react-hooks/refs
  cbRef.current = callback;

  useEffect(() => {
    if (!token) return;

    if (!subscribers.has(key)) subscribers.set(key, new Set());
    const cb: Callback = (data) => cbRef.current(data);
    subscribers.get(key)!.add(cb);

    connect(token);
    if (authenticated) subscribeSymbols();

    return () => {
      subscribers.get(key)?.delete(cb);
      if (!subscribers.get(key)?.size) subscribers.delete(key);
      if (subscribers.size === 0) disconnect();
    };
  }, [key, token]);
}

// Shared no-op: useLiveQuote consumers get their updates through the
// quoteStore selector, not the legacy callback path.
const NOOP: Callback = () => {};

/**
 * Subscribe to one instrument's live quote (blueprint §4.6).
 *
 * Registers the `"SYMBOL:MARKET"` WS subscription (so the backend
 * streams it) and reads the rAF-batched quoteStore via a per-key
 * selector — the component re-renders only when ITS symbol's record
 * changes, never on other symbols' ticks.
 *
 * Returns `undefined` until the first tick lands; callers fall back to
 * their REST-fetched quote for initial paint.
 */
export function useLiveQuote(symbol: string, market: string): LiveQuote | undefined {
  const key = `${symbol}:${market}`;
  useWebSocket(key, NOOP);
  return useQuote(key);
}

export function useWsConnected(): boolean {
  const [connected, setConnected] = useState(wsConnected);
  useEffect(() => {
    wsStateListeners.add(setConnected);
    return () => { wsStateListeners.delete(setConnected); };
  }, []);
  return connected;
}

export function useAlertSocket(callback: AlertCallback): void {
  const token = useAuthStore((s) => s.token);
  const cbRef = useRef(callback);
  // eslint-disable-next-line react-hooks/refs
  cbRef.current = callback;

  useEffect(() => {
    if (!token) return;
    const cb: AlertCallback = (alert) => cbRef.current(alert);
    alertCallbacks.add(cb);
    connect(token);
    return () => { alertCallbacks.delete(cb); };
  }, [token]);
}
