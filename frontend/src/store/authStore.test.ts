import { describe, it, expect, beforeEach } from "vitest";
import { useAuthStore } from "./authStore";

describe("authStore", () => {
  beforeEach(() => {
    useAuthStore.getState().clearAuth();
  });

  it("starts unauthenticated", () => {
    const s = useAuthStore.getState();
    expect(s.token).toBeNull();
    expect(s.user).toBeNull();
    expect(s.isAuthenticated()).toBe(false);
  });

  it("setAuth stores token + user and flips isAuthenticated", () => {
    const user = { id: "abc", email: "u@test.com", role: "viewer" as const };
    useAuthStore.getState().setAuth("token-xyz", user);

    const s = useAuthStore.getState();
    expect(s.token).toBe("token-xyz");
    expect(s.user).toEqual(user);
    expect(s.isAuthenticated()).toBe(true);
  });

  it("setToken updates token without touching user (refresh flow)", () => {
    const user = { id: "abc", email: "u@test.com", role: "analyst" as const };
    useAuthStore.getState().setAuth("old-token", user);
    useAuthStore.getState().setToken("new-token");

    const s = useAuthStore.getState();
    expect(s.token).toBe("new-token");
    expect(s.user).toEqual(user);
  });

  it("clearAuth resets both fields", () => {
    useAuthStore.getState().setAuth("t", { id: "1", email: "a@b", role: "admin" });
    useAuthStore.getState().clearAuth();

    const s = useAuthStore.getState();
    expect(s.token).toBeNull();
    expect(s.user).toBeNull();
    expect(s.isAuthenticated()).toBe(false);
  });
});
