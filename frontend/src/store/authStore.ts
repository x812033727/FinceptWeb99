import { create } from "zustand";

interface User {
  id: string;
  email: string;
  role: "viewer" | "analyst" | "admin";
}

interface AuthState {
  token: string | null;
  user: User | null;
  setAuth: (token: string, user: User) => void;
  clearAuth: () => void;
}

// Token stored in memory only (not localStorage) to prevent XSS.
// Refresh token is stored in httpOnly cookie set by the backend (Phase 1).
export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,
  setAuth: (token, user) => set({ token, user }),
  clearAuth: () => set({ token: null, user: null }),
}));
