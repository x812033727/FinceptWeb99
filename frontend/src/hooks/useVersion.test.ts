/**
 * Unit tests for useVersion / useTriggerUpdate / useCheckForUpdates.
 *
 * Each hook is a thin TanStack Query wrapper. Tests verify:
 *  - which endpoint is called,
 *  - the query key used (so cache invalidation can target it),
 *  - what the mutation does to the cache on success — useTriggerUpdate
 *    invalidates (re-fetches), useCheckForUpdates writes directly via
 *    setQueryData (skips a network round-trip after the manual check).
 */
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AxiosMockAdapter from "axios-mock-adapter";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import api from "@/lib/api";
import { useCheckForUpdates, useTriggerUpdate, useVersion } from "./useVersion";
import type { UpdateResult, VersionStatus } from "@/types/system";

const mock = new AxiosMockAdapter(api);

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    qc,
    wrapper: ({ children }: { children: React.ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children),
  };
}

const sampleStatus: VersionStatus = {
  current: "0.2.2",
  latest: "0.3.0",
  update_available: true,
  html_url: "https://github.com/x812033727/FinceptWeb/releases/tag/v0.3.0",
  published_at: "2024-04-01T00:00:00Z",
};

beforeEach(() => mock.reset());
afterEach(() => mock.reset());

// ── useVersion ───────────────────────────────────────────────────

describe("useVersion", () => {
  it("calls GET /api/system/version and returns the VersionStatus", async () => {
    mock.onGet("/system/version").reply(200, sampleStatus);
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useVersion(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(sampleStatus);
  });

  it("uses ['version'] as the queryKey so mutations can target the cache", async () => {
    mock.onGet("/system/version").reply(200, sampleStatus);
    const { qc, wrapper } = makeWrapper();
    renderHook(() => useVersion(), { wrapper });

    // Once the query resolves, the cache entry must be addressable
    // under the ["version"] key — useTriggerUpdate / useCheckForUpdates
    // both rely on this exact key for invalidate / setQueryData.
    await waitFor(() => expect(qc.getQueryData(["version"])).toEqual(sampleStatus));
  });

  it("surfaces errors when the API fails", async () => {
    mock.onGet("/system/version").reply(500);
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useVersion(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

// ── useTriggerUpdate ─────────────────────────────────────────────

describe("useTriggerUpdate", () => {
  it("POSTs /api/admin/update and returns the UpdateResult", async () => {
    const result_body: UpdateResult = { status: "started", message: "ok" };
    mock.onPost("/admin/update").reply(200, result_body);

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useTriggerUpdate(), { wrapper });

    let returned: UpdateResult | undefined;
    await act(async () => {
      returned = await result.current.mutateAsync();
    });
    // mutateAsync() returns the data directly; result.current.data
    // also lands eventually but may not be in sync immediately after
    // the await.
    expect(returned).toEqual(result_body);
    await waitFor(() => expect(result.current.data).toEqual(result_body));
  });

  it("invalidates the ['version'] query on success so the badge re-fetches", async () => {
    // Prime the version cache so we can observe whether invalidation
    // refetches it.
    mock.onGet("/system/version").reply(200, sampleStatus);
    mock.onPost("/admin/update").reply(200, { status: "started", message: "ok" });

    const { qc, wrapper } = makeWrapper();
    renderHook(() => useVersion(), { wrapper });
    await waitFor(() => expect(qc.getQueryData(["version"])).toBeTruthy());

    // Now run the mutation and assert the version fetch happened again.
    const versionFetchesBefore = mock.history.get.filter((c) => c.url === "/system/version").length;
    const { result: trigger } = renderHook(() => useTriggerUpdate(), { wrapper });
    await act(async () => {
      await trigger.current.mutateAsync();
    });
    await waitFor(() => {
      const after = mock.history.get.filter((c) => c.url === "/system/version").length;
      expect(after).toBeGreaterThan(versionFetchesBefore);
    });
  });

  it("propagates the error when the update endpoint fails", async () => {
    mock.onPost("/admin/update").reply(500);
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useTriggerUpdate(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync().catch(() => {});
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

// ── useCheckForUpdates ───────────────────────────────────────────

describe("useCheckForUpdates", () => {
  it("POSTs /api/admin/version/check and returns the VersionStatus", async () => {
    mock.onPost("/admin/version/check").reply(200, sampleStatus);
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCheckForUpdates(), { wrapper });

    let returned: VersionStatus | undefined;
    await act(async () => {
      returned = await result.current.mutateAsync();
    });
    expect(returned).toEqual(sampleStatus);
    await waitFor(() => expect(result.current.data).toEqual(sampleStatus));
  });

  it("writes the response straight into the ['version'] cache (no extra GET)", async () => {
    mock.onPost("/admin/version/check").reply(200, sampleStatus);

    const { qc, wrapper } = makeWrapper();
    expect(qc.getQueryData(["version"])).toBeUndefined();

    const { result } = renderHook(() => useCheckForUpdates(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync();
    });

    // Cache populated via setQueryData — not a network refetch.
    expect(qc.getQueryData(["version"])).toEqual(sampleStatus);
    expect(mock.history.get.filter((c) => c.url === "/system/version")).toHaveLength(0);
  });
});
