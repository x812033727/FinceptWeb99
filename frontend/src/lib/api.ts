import axios, { AxiosError } from "axios";
import { useAuthStore } from "@/store/authStore";
import { useToastStore } from "@/store/toastStore";

const api = axios.create({
  baseURL: "/api",
  withCredentials: true,   // send httpOnly refresh_token cookie
});

// ── Request interceptor: attach Bearer token ──────────────────────
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ── 429 helper ────────────────────────────────────────────────────
// Surfaces rate-limit / quota errors as a toast so users see them even
// when the call site only catches the rejected promise. Used by both
// the axios interceptor below and the SSE fetch path in AIPage.

// Server-supplied error strings are rendered as plain text via React (no
// dangerouslySetInnerHTML), so XSS is already prevented; the slice is purely
// to stop a hostile/buggy backend from blowing up the toast UI.
const MAX_DETAIL_LEN = 200;

function safeDetail(input: unknown): string | undefined {
  if (typeof input !== "string" || !input) return undefined;
  return input.length > MAX_DETAIL_LEN ? input.slice(0, MAX_DETAIL_LEN) + "…" : input;
}

export function notifyRateLimited(detail?: string, retryAfterSec?: number): void {
  const safe = safeDetail(detail);
  const isAiQuota = !!safe && /ai quota/i.test(safe);
  const title = isAiQuota ? "AI quota exceeded" : "Rate limit reached";
  const fallback = isAiQuota
    ? "Daily AI request limit reached. Resets at midnight UTC."
    : "Too many requests — please slow down and try again shortly.";
  const message = safe || fallback;
  const retry = retryAfterSec
    ? ` Retry in ~${retryAfterSec}s.`
    : "";
  useToastStore.getState().push({
    severity: "warning",
    title,
    detail: message + retry,
    ttlMs: 8000,
  });
}

// ── Response interceptor: auto-refresh on 401, toast on 429 ──────
let refreshing: Promise<string> | null = null;

// Defence-in-depth alongside the per-request `_retry` flag — if a future
// edit ever removes that flag, the counter still bounds total retries per
// request to a safe ceiling.
const MAX_RETRIES = 1;

api.interceptors.response.use(
  (res) => res,
  async (err: AxiosError) => {
    const original = err.config!;
    const status = err.response?.status;

    if (status === 429) {
      const data = err.response?.data as { detail?: string } | undefined;
      const retryAfter = Number(err.response?.headers?.["retry-after"]) || undefined;
      notifyRateLimited(data?.detail, retryAfter);
      return Promise.reject(err);
    }

    // Only retry on 401 (not on the refresh endpoint itself), and never
    // more than MAX_RETRIES times per request.
    const retryCount = ((original as { _retryCount?: number })._retryCount) ?? 0;
    if (status !== 401 || retryCount >= MAX_RETRIES) {
      return Promise.reject(err);
    }
    if (original.url?.includes("/auth/refresh")) {
      useAuthStore.getState().clearAuth();
      return Promise.reject(err);
    }

    (original as { _retryCount?: number })._retryCount = retryCount + 1;

    // Deduplicate concurrent refresh calls
    if (!refreshing) {
      refreshing = axios
        .post<{ access_token: string }>("/api/auth/refresh", {}, { withCredentials: true })
        .then((r) => {
          const token = r.data.access_token;
          useAuthStore.getState().setToken(token);
          return token;
        })
        .catch((e) => {
          useAuthStore.getState().clearAuth();
          return Promise.reject(e);
        })
        .finally(() => {
          refreshing = null;
        });
    }

    const newToken = await refreshing;
    original.headers!.Authorization = `Bearer ${newToken}`;
    return api(original);
  }
);

export default api;
