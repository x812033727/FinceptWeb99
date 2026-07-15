import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAuthStore } from "@/store/authStore";
import { clearQueryCacheOnAuthChange } from "./authQueryCache";

const USER_A = { id: "user-a", email: "a@example.com", role: "viewer" as const };
const USER_B = { id: "user-b", email: "b@example.com", role: "analyst" as const };

let unsubscribe: (() => void) | undefined;

beforeEach(() => {
  useAuthStore.getState().clearAuth();
});

afterEach(() => {
  unsubscribe?.();
  unsubscribe = undefined;
  useAuthStore.getState().clearAuth();
});

function clientWithPrivateData() {
  const client = new QueryClient();
  client.setQueryData(["portfolios"], [{ id: "private-portfolio" }]);
  return client;
}

describe("clearQueryCacheOnAuthChange", () => {
  it("clears owner-scoped queries when the user signs out", () => {
    useAuthStore.getState().setAuth("token-a", USER_A);
    const client = clientWithPrivateData();
    unsubscribe = clearQueryCacheOnAuthChange(client);

    useAuthStore.getState().clearAuth();

    expect(client.getQueryData(["portfolios"])).toBeUndefined();
  });

  it("clears cached data when one signed-in account replaces another", () => {
    useAuthStore.getState().setAuth("token-a", USER_A);
    const client = clientWithPrivateData();
    unsubscribe = clearQueryCacheOnAuthChange(client);

    useAuthStore.getState().setAuth("token-b", USER_B);

    expect(client.getQueryData(["portfolios"])).toBeUndefined();
  });

  it("preserves cached data during access-token rotation for the same user", () => {
    useAuthStore.getState().setAuth("old-token", USER_A);
    const client = clientWithPrivateData();
    unsubscribe = clearQueryCacheOnAuthChange(client);

    useAuthStore.getState().setToken("new-token");
    useAuthStore.getState().setAuth("new-token", USER_A);

    expect(client.getQueryData(["portfolios"])).toEqual([
      { id: "private-portfolio" },
    ]);
  });
});
