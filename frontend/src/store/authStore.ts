import { create } from "zustand";

interface User {
  id: string;
  email: string;
  role: "viewer" | "analyst" | "admin";
  ai_requests_remaining?: number;
}

interface AuthState {
  token: string | null;
  user: User | null;
  setAuth: (token: string, user: User) => void;
  setToken: (token: string) => void;   // used by refresh interceptor
  clearAuth: () => void;
  isAuthenticated: () => boolean;
}

// Access token in memory only — prevents XSS token theft.
// Refresh token lives in httpOnly cookie managed by the browser.
export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  setAuth: (token, user) => set({ token, user }),
  setToken: (token) => set({ token }),
  clearAuth: () => set({ token: null, user: null }),
  isAuthenticated: () => !!get().token,
}));
