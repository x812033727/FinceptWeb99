import api from "./api";
import { useAuthStore } from "@/store/authStore";
import { disablePush } from "@/lib/webPush";

export async function login(email: string, password: string): Promise<void> {
  const { data } = await api.post<{ access_token: string }>("/auth/login", { email, password });
  const me = await api.get("/auth/me", {
    headers: { Authorization: `Bearer ${data.access_token}` },
  });
  useAuthStore.getState().setAuth(data.access_token, me.data);
}

async function establishSession(data: { access_token: string }): Promise<void> {
  const me = await api.get("/auth/me", {
    headers: { Authorization: `Bearer ${data.access_token}` },
  });
  useAuthStore.getState().setAuth(data.access_token, me.data);
}

export async function acceptInvite(token: string, email: string, password: string): Promise<void> {
  const { data } = await api.post<{ access_token: string }>("/auth/accept-invite", {
    token, email, password,
  });
  await establishSession(data);
}

export async function forgotPassword(email: string): Promise<void> {
  await api.post("/auth/password/forgot", { email });
}

export async function resetPassword(token: string, newPassword: string): Promise<void> {
  await api.post("/auth/password/reset", { token, new_password: newPassword });
}

export async function logout(): Promise<void> {
  // Revoke this device's push endpoint while the access token still belongs
  // to the outgoing user. Both operations are best-effort: local auth state
  // must always be cleared even if either network/browser API is unavailable.
  await Promise.allSettled([
    disablePush(),
    api.post("/auth/logout"),
  ]);
  useAuthStore.getState().clearAuth();
}

let silentRefreshPromise: Promise<void> | null = null;

async function refreshSession(): Promise<void> {
  try {
    const { data } = await api.post<{ access_token: string }>("/auth/refresh");
    useAuthStore.getState().setToken(data.access_token);
    const me = await api.get("/auth/me");
    useAuthStore.getState().setAuth(data.access_token, me.data);
  } catch {
    // No valid refresh cookie — user must log in
    useAuthStore.getState().clearAuth();
  }
}

/**
 * Attempt a silent refresh using the httpOnly cookie on app load.
 *
 * React StrictMode intentionally re-runs effects in development. Refresh
 * tokens are rotated by the backend, so concurrent boot requests would race:
 * one succeeds while the other reuses the revoked cookie and clears the
 * newly-established session. Share the in-flight operation so every caller
 * observes the same result and only one rotating request reaches the server.
 */
export function silentRefresh(): Promise<void> {
  if (silentRefreshPromise) return silentRefreshPromise;

  silentRefreshPromise = refreshSession().finally(() => {
    silentRefreshPromise = null;
  });
  return silentRefreshPromise;
}
