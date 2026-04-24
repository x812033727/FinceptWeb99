/**
 * Manages a single WebSocket connection per user session.
 * Components call subscribe(key, cb) / unsubscribe(key, cb) in useEffect.
 * Reconnects automatically with exponential backoff (1s → 2s → 4s → max 30s).
 *
 * key format: "SYMBOL:MARKET"  e.g. "AAPL:US" | "2330:TW"
 */
import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "@/store/authStore";

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
let authenticated = false;
let wsConnected = false;

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

function connect(token: string): void {
  if (socket && socket.readyState !== WebSocket.CLOSED) return;

  socket = new WebSocket(getWsUrl());
  authenticated = false;

  socket.onopen = () => {
    reconnectDelay = 1000;
    notifyWsState(true);
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
    } catch {
      // ignore parse errors
    }
  };

  socket.onclose = () => {
    authenticated = false;
    socket = null;
    notifyWsState(false);
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
  notifyWsState(false);
}

// ── React hooks ───────────────────────────────────────────────────

export function useWebSocket(key: string, callback: Callback): void {
  const token = useAuthStore((s) => s.token);
  const cbRef = useRef(callback);
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
  cbRef.current = callback;

  useEffect(() => {
    if (!token) return;
    const cb: AlertCallback = (alert) => cbRef.current(alert);
    alertCallbacks.add(cb);
    connect(token);
    return () => { alertCallbacks.delete(cb); };
  }, [token]);
}
