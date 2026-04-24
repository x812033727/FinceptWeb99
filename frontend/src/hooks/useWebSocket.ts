/**
 * Manages a single WebSocket connection per user session.
 * Components call subscribe(key, cb) / unsubscribe(key, cb) in useEffect.
 * Reconnects automatically with exponential backoff (1s → 2s → 4s → max 30s).
 *
 * key format: "SYMBOL:MARKET"  e.g. "AAPL:US" | "2330:TW"
 */
import { useEffect, useRef } from "react";
import { useAuthStore } from "@/store/authStore";

type Callback = (data: unknown) => void;

const subscribers = new Map<string, Set<Callback>>();
let socket: WebSocket | null = null;
let reconnectDelay = 1000;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let authenticated = false;

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

function connect(token: string): void {
  if (socket && socket.readyState !== WebSocket.CLOSED) return;

  socket = new WebSocket(getWsUrl());
  authenticated = false;

  socket.onopen = () => {
    reconnectDelay = 1000;
    sendJson({ action: "auth", token });
  };

  socket.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data as string);

      if (msg.type === "snapshot") {
        authenticated = true;
        for (const [key, data] of Object.entries(msg.data ?? {})) {
          subscribers.get(key)?.forEach((cb) => cb(data));
        }
        return;
      }

      if (msg.type === "delta") {
        const key = `${msg.symbol}:${msg.market}`;
        subscribers.get(key)?.forEach((cb) => cb(msg.data));
        return;
      }

      if (msg.type === "ping") {
        sendJson({ action: "pong" });
        // After first auth+snapshot cycle, re-subscribe on reconnect
        if (!authenticated) {
          authenticated = true;
          subscribeSymbols();
        }
        return;
      }
    } catch {
      // ignore parse errors
    }
  };

  socket.onclose = () => {
    authenticated = false;
    socket = null;
    // Reconnect with exponential backoff
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => {
      const t = useAuthStore.getState().token;
      if (t) connect(t);
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
  };

  socket.onerror = () => socket?.close();
}

function disconnect(): void {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  socket?.close();
  socket = null;
  authenticated = false;
}

// ── React hook ────────────────────────────────────────────────────

export function useWebSocket(key: string, callback: Callback): void {
  const token = useAuthStore((s) => s.token);
  const cbRef = useRef(callback);
  cbRef.current = callback;

  useEffect(() => {
    if (!token) return;

    // Register subscriber
    if (!subscribers.has(key)) subscribers.set(key, new Set());
    const cb: Callback = (data) => cbRef.current(data);
    subscribers.get(key)!.add(cb);

    // Ensure connection exists
    connect(token);

    // If already authenticated, send subscribe immediately
    if (authenticated) subscribeSymbols();

    return () => {
      subscribers.get(key)?.delete(cb);
      if (!subscribers.get(key)?.size) subscribers.delete(key);
      if (subscribers.size === 0) disconnect();
    };
  }, [key, token]);
}
