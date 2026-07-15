import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/store/authStore";
import { useSessionAbortController } from "./useSessionAbortController";

const USER_A = { id: "user-a", email: "a@example.com", role: "viewer" as const };
const USER_B = { id: "user-b", email: "b@example.com", role: "viewer" as const };

beforeEach(() => {
  useAuthStore.getState().clearAuth();
  useAuthStore.getState().setAuth("token-a", USER_A);
});

describe("useSessionAbortController", () => {
  it("aborts the previous request when a new one starts", () => {
    const { result } = renderHook(() => useSessionAbortController());
    let first!: AbortController;
    act(() => { first = result.current.renew(); });

    act(() => { result.current.renew(); });

    expect(first.signal.aborted).toBe(true);
  });

  it("preserves an in-flight request across same-user token rotation", () => {
    const { result } = renderHook(() => useSessionAbortController());
    let controller!: AbortController;
    act(() => { controller = result.current.renew(); });

    act(() => useAuthStore.getState().setToken("token-refreshed"));

    expect(controller.signal.aborted).toBe(false);
  });

  it("aborts on account replacement and logout", () => {
    const { result } = renderHook(() => useSessionAbortController());
    let accountRequest!: AbortController;
    act(() => { accountRequest = result.current.renew(); });

    act(() => useAuthStore.getState().setAuth("token-b", USER_B));
    expect(accountRequest.signal.aborted).toBe(true);

    let logoutRequest!: AbortController;
    act(() => { logoutRequest = result.current.renew(); });
    act(() => useAuthStore.getState().clearAuth());
    expect(logoutRequest.signal.aborted).toBe(true);
  });

  it("aborts when the owning component unmounts", () => {
    const { result, unmount } = renderHook(() => useSessionAbortController());
    let controller!: AbortController;
    act(() => { controller = result.current.renew(); });

    unmount();

    expect(controller.signal.aborted).toBe(true);
  });
});
