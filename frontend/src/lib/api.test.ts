import { describe, it, expect, beforeEach, vi } from "vitest";
import MockAdapter from "axios-mock-adapter";
import axios from "axios";
import api from "./api";
import { useAuthStore } from "@/store/authStore";
import { useToastStore } from "@/store/toastStore";

describe("api axios instance", () => {
  let mock: MockAdapter;
  // The refresh call is made with the raw `axios` default instance, not `api`,
  // so we mount a mock on both to cover every path.
  let rootMock: MockAdapter;

  beforeEach(() => {
    mock = new MockAdapter(api);
    rootMock = new MockAdapter(axios);
    useAuthStore.getState().clearAuth();
    useToastStore.setState({ toasts: [] });
  });

  it("baseURL is /api (so callers must not include the prefix)", () => {
    expect(api.defaults.baseURL).toBe("/api");
  });

  it("attaches Bearer token from authStore when present", async () => {
    useAuthStore.getState().setAuth("tok-123", {
      id: "u1", email: "a@b", role: "viewer",
    });
    mock.onGet("/auth/me").reply((cfg) => {
      expect(cfg.headers?.Authorization).toBe("Bearer tok-123");
      return [200, { ok: true }];
    });

    const r = await api.get("/auth/me");
    expect(r.data).toEqual({ ok: true });
  });

  it("does not attach Authorization when no token is set", async () => {
    mock.onGet("/public/ping").reply((cfg) => {
      expect(cfg.headers?.Authorization).toBeUndefined();
      return [200, { ok: true }];
    });
    await api.get("/public/ping");
  });

  it("on 401 → calls /auth/refresh, updates token, and retries original request", async () => {
    useAuthStore.getState().setAuth("old-token", {
      id: "u1", email: "a@b", role: "viewer",
    });

    let callCount = 0;
    mock.onGet("/portfolio").reply(() => {
      callCount += 1;
      const currentToken = useAuthStore.getState().token;
      if (currentToken === "old-token") return [401, { detail: "expired" }];
      return [200, { data: "ok" }];
    });
    rootMock.onPost("/api/auth/refresh").reply(200, { access_token: "new-token" });

    const r = await api.get("/portfolio");
    expect(r.status).toBe(200);
    expect(callCount).toBe(2);
    expect(useAuthStore.getState().token).toBe("new-token");
  });

  it("failed refresh clears auth and propagates the original 401", async () => {
    useAuthStore.getState().setAuth("old-token", {
      id: "u1", email: "a@b", role: "viewer",
    });
    mock.onGet("/portfolio").reply(401, { detail: "expired" });
    rootMock.onPost("/api/auth/refresh").reply(401, { detail: "no refresh" });

    await expect(api.get("/portfolio")).rejects.toMatchObject({
      response: { status: 401 },
    });
    expect(useAuthStore.getState().token).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });

  it("concurrent 401s trigger only one refresh call (deduplication)", async () => {
    useAuthStore.getState().setAuth("old-token", {
      id: "u1", email: "a@b", role: "viewer",
    });

    mock.onGet("/a").reply(() => {
      return useAuthStore.getState().token === "old-token"
        ? [401, {}]
        : [200, { path: "a" }];
    });
    mock.onGet("/b").reply(() => {
      return useAuthStore.getState().token === "old-token"
        ? [401, {}]
        : [200, { path: "b" }];
    });

    const refreshSpy = vi.fn(() => [200, { access_token: "new-token" }] as [number, object]);
    rootMock.onPost("/api/auth/refresh").reply(refreshSpy);

    const [ra, rb] = await Promise.all([api.get("/a"), api.get("/b")]);
    expect(ra.data).toEqual({ path: "a" });
    expect(rb.data).toEqual({ path: "b" });
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });

  it("on 429 → pushes a warning toast and rejects the promise", async () => {
    mock.onGet("/ai/chat").reply(429, {
      detail: "Daily AI quota exceeded (5 requests/day). Resets at midnight UTC.",
    });

    await expect(api.get("/ai/chat")).rejects.toMatchObject({
      response: { status: 429 },
    });

    const toasts = useToastStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].severity).toBe("warning");
    expect(toasts[0].title).toBe("AI quota exceeded");
    expect(toasts[0].detail).toContain("Resets at midnight UTC");
  });

  it("429 with non-quota detail uses generic title", async () => {
    mock.onGet("/us/screener").reply(429, { detail: "Too many requests" });

    await expect(api.get("/us/screener")).rejects.toMatchObject({
      response: { status: 429 },
    });

    const toasts = useToastStore.getState().toasts;
    expect(toasts[0].title).toBe("Rate limit reached");
    expect(toasts[0].detail).toContain("Too many requests");
  });
});
