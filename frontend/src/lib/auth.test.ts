import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock, disablePushMock } = vi.hoisted(() => ({
  apiMock: { post: vi.fn(), get: vi.fn() },
  disablePushMock: vi.fn(),
}));

vi.mock("./api", () => ({ default: apiMock }));
vi.mock("@/lib/webPush", () => ({ disablePush: disablePushMock }));

import { logout, silentRefresh } from "./auth";
import { useAuthStore } from "@/store/authStore";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.post.mockResolvedValue({});
  disablePushMock.mockResolvedValue("off");
  useAuthStore.getState().setAuth("token-a", {
    id: "user-a", email: "a@example.com", role: "viewer",
  });
});

describe("logout", () => {
  it("revokes the device push subscription and server session before clearing auth", async () => {
    await logout();

    expect(disablePushMock).toHaveBeenCalledTimes(1);
    expect(apiMock.post).toHaveBeenCalledWith("/auth/logout");
    expect(useAuthStore.getState()).toMatchObject({ token: null, user: null });
  });

  it("always clears local auth when push and server cleanup fail", async () => {
    disablePushMock.mockRejectedValue(new Error("push unavailable"));
    apiMock.post.mockRejectedValue(new Error("network unavailable"));

    await expect(logout()).resolves.toBeUndefined();

    expect(useAuthStore.getState()).toMatchObject({ token: null, user: null });
  });
});

describe("silentRefresh", () => {
  it("shares one rotating refresh request between concurrent boot callers", async () => {
    let resolveRefresh!: (value: { data: { access_token: string } }) => void;
    const refreshResponse = new Promise<{ data: { access_token: string } }>((resolve) => {
      resolveRefresh = resolve;
    });
    apiMock.post.mockReturnValueOnce(refreshResponse);
    apiMock.get.mockResolvedValueOnce({
      data: { id: "user-b", email: "b@example.com", role: "analyst" },
    });

    const first = silentRefresh();
    const second = silentRefresh();

    expect(second).toBe(first);
    expect(apiMock.post).toHaveBeenCalledTimes(1);
    expect(apiMock.post).toHaveBeenCalledWith("/auth/refresh");

    resolveRefresh({ data: { access_token: "refreshed-token" } });
    await Promise.all([first, second]);

    expect(apiMock.get).toHaveBeenCalledTimes(1);
    expect(apiMock.get).toHaveBeenCalledWith("/auth/me");
    expect(useAuthStore.getState()).toMatchObject({
      token: "refreshed-token",
      user: { id: "user-b", email: "b@example.com", role: "analyst" },
    });
  });

  it("allows a later boot attempt after the in-flight refresh settles", async () => {
    apiMock.post
      .mockResolvedValueOnce({ data: { access_token: "first-token" } })
      .mockResolvedValueOnce({ data: { access_token: "second-token" } });
    apiMock.get
      .mockResolvedValueOnce({
        data: { id: "user-b", email: "b@example.com", role: "analyst" },
      })
      .mockResolvedValueOnce({
        data: { id: "user-b", email: "b@example.com", role: "analyst" },
      });

    await silentRefresh();
    await silentRefresh();

    expect(apiMock.post).toHaveBeenCalledTimes(2);
    expect(useAuthStore.getState().token).toBe("second-token");
  });
});
