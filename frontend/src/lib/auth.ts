import api from "./api";
import { useAuthStore } from "@/store/authStore";

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
  await api.post("/auth/logout").catch(() => null);
  useAuthStore.getState().clearAuth();
}

/** Attempt a silent refresh using the httpOnly cookie on app load. */
export async function silentRefresh(): Promise<void> {
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
