import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiMock, disablePushMock } = vi.hoisted(() => ({
  apiMock: { post: vi.fn(), get: vi.fn() },
  disablePushMock: vi.fn(),
}));

vi.mock("./api", () => ({ default: apiMock }));
vi.mock("@/lib/webPush", () => ({ disablePush: disablePushMock }));

import { logout } from "./auth";
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
