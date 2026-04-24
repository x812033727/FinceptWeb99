import axios, { AxiosError } from "axios";
import { useAuthStore } from "@/store/authStore";

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

// ── Response interceptor: auto-refresh on 401 ────────────────────
let refreshing: Promise<string> | null = null;

api.interceptors.response.use(
  (res) => res,
  async (err: AxiosError) => {
    const original = err.config!;
    // Only retry once and only on 401 (not on the refresh endpoint itself)
    if (err.response?.status !== 401 || (original as any)._retry) {
      return Promise.reject(err);
    }
    if (original.url?.includes("/auth/refresh")) {
      useAuthStore.getState().clearAuth();
      return Promise.reject(err);
    }

    (original as any)._retry = true;

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
